import json
from pathlib import Path
from typing import Dict, Any, Set, Optional

from fastapi import FastAPI, Request, Query, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import httpx

# ---- 기본 설정 ----

BASE_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "workbooks.json", "r", encoding="utf-8") as f:
    WORKBOOKS: Dict[str, Any] = json.load(f)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="BOJ Workbook Progress Viewer")

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


ADMIN_KEY = "lenamayer28"


# ---- solved.ac 클라이언트 ----

class SolvedAcClient:
    BASE_URL = "https://solved.ac/api/v3"

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=10.0,
            headers={"User-Agent": "boj-workbook-progress/0.1"},
        )

    async def get_solved_set(self, handle: str) -> Set[int]:
        solved: Set[int] = set()
        page = 1

        while True:
            params = {"query": f"solved_by:{handle}", "page": page}
            resp = await self.client.get("/search/problem", params=params)
            resp.raise_for_status()
            data = resp.json()

            items = data.get("items", [])
            for item in items:
                pid = item.get("problemId")
                if pid:
                    solved.add(pid)

            total = data.get("count", 0)
            per_page = len(items)

            if per_page == 0 or page * per_page >= total:
                break

            page += 1

        return solved

    async def problem_exists(self, problem_id: int) -> bool:
        """
        BOJ 문제 번호가 실제로 존재하는지 solved.ac를 통해 확인.
        존재하면 True, 없으면 False.
        """
        try:
            resp = await self.client.get(
                "/problem/show",
                params={"problemId": problem_id},
            )
        except httpx.HTTPError:
            # 네트워크 오류 등은 일단 "존재하지 않는다"로 처리 (관리자에게 다시 시도 유도)
            return False

        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            # 기타 이상 상태 코드도 안전하게 False 처리
            return False

        return True
    async def get_problem_info(self, problem_id: int) -> Optional[Dict[str, Any]]:
        '''문제 정보(이름, 난이도 등) 가져옴. 실패하면 None.'''
        try:
            resp = await self.client.get(
                "/problem/show",
                params={"problemId": problem_id},
            )
        except httpx.HTTPError:
            return None
        
        if resp.status_code != 200:
            return None
        
        data = resp.json()
        if not data:
            return None
        
        return data   # solved.ac는 단일 항목도 list 형태로 반환

    async def close(self):
        await self.client.aclose()


solvedac_client = SolvedAcClient()


@app.on_event("shutdown")
async def shutdown_event():
    await solvedac_client.close()


# ---- 유틸 함수 ----
TIER_NAMES = ["브론즈", "실버", "골드", "플래티넘", "다이아몬드", "루비"]

def convert_tier(tier: int) -> str:
    if tier == 0:
        return "미분류0"
    group = (tier - 1) // 5   # 0=브론즈 ~ 5=루비
    level = 5 - ((tier - 1) % 5)  # 5~1
    return f"{TIER_NAMES[group]}{level}"

async def compute_progress(handle: str, workbook_key: str, solved_set: Set[int]):
    wb = WORKBOOKS[workbook_key]
    problems = wb["problems"]

    solved_in = []
    unsolved_in = []

    for pid in problems:
        info = await solvedac_client.get_problem_info(pid)
        if info:
            item = info[0]  # solved.ac는 리스트로 반환
            name = item["titleKo"]
            tier = convert_tier(item["level"])
        else:
            name = "(알 수 없음)"
            tier = "미분류0"

        entry = {
            "pid": pid,
            "name": name,
            "tier": tier,
        }

        if pid in solved_set:
            solved_in.append(entry)
        else:
            unsolved_in.append(entry)

    total = len(problems)
    solved_cnt = len(solved_in)
    rate = solved_cnt / total * 100 if total else 0

    return {
        "handle": handle,
        "workbook_key": workbook_key,
        "workbook_name": wb["name"],
        "total": total,
        "solved_cnt": solved_cnt,
        "rate": rate,
        "solved_list": solved_in,
        "unsolved_list": unsolved_in,
    }

def save_workbooks_to_file() -> None:
    """WORKBOOKS 딕셔너리를 workbooks.json 파일에 저장."""
    with open(BASE_DIR / "workbooks.json", "w", encoding="utf-8") as f:
        json.dump(WORKBOOKS, f, ensure_ascii=False, indent=2)


# ---- 학생 진도 조회 (기존 기능) ----

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    handle: Optional[str] = Query(default=None),
    workbook: Optional[str] = Query(default=None),
):
    progress = None
    error = None

    if handle and workbook:
        if workbook not in WORKBOOKS:
            error = "문제집 키가 잘못되었습니다."
        else:
            try:
                solved_set = await solvedac_client.get_solved_set(handle)
                progress = compute_progress(handle, workbook, solved_set)
            except Exception as e:
                error = f"오류 발생: {str(e)}"

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "workbooks": WORKBOOKS,
            "selected_handle": handle or "",
            "selected_workbook": workbook or "",
            "progress": progress,
            "error": error,
            # 관리자 영역 메시지 (기본값 None)
            "admin_error": None,
            "admin_message": None,
        },
    )


# ---- 관리자: 문제집에 문제 추가 ----

@app.post("/admin/add_problem", response_class=HTMLResponse)
async def admin_add_problem(
    request: Request,
    admin_key: str = Form(...),
    problem_id: str = Form(...),
    workbook: str = Form(...),
):
    progress = None
    error = None
    admin_error = None
    admin_message = None

    # 1) 관리자 키 검증
    if admin_key != ADMIN_KEY:
        admin_error = "관리자 키가 올바르지 않습니다."
    else:
        # 2) 문제 번호 입력 검증
        problem_id = problem_id.strip()
        if not problem_id.isdecimal():
            admin_error = "문제 번호는 1000 이상의 정수여야 합니다."
        else:
            pid_int = int(problem_id)
            if pid_int < 1000:
                admin_error = "문제 번호는 1000 이상의 정수여야 합니다."
            else:
                # 3) 문제집 키 검증 (화이트리스트)
                if workbook not in WORKBOOKS:
                    admin_error = "존재하지 않는 문제집입니다."
                else:
                    # 4) solved.ac로 문제 존재 여부 확인
                    exists = await solvedac_client.problem_exists(pid_int)
                    if not exists:
                        admin_error = "BOJ에 존재하지 않는 문제 번호입니다."
                    else:
                        # 5) 중복 여부 확인 후 추가
                        problems = WORKBOOKS[workbook]["problems"]
                        if pid_int in problems:
                            admin_message = f"이미 문제집에 포함된 문제입니다: {pid_int}"
                        else:
                            problems.append(pid_int)
                            # 정렬(optional): 항상 오름차순 유지하고 싶다면
                            problems.sort()
                            save_workbooks_to_file()
                            admin_message = f"문제 {pid_int} 이(가) '{WORKBOOKS[workbook]['name']}' 문제집에 추가되었습니다."

    # 학생 진도 조회는 이 요청에서는 수행하지 않음(progress=None 유지)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "workbooks": WORKBOOKS,
            "selected_handle": "",
            "selected_workbook": workbook if workbook in WORKBOOKS else "",
            "progress": progress,
            "error": error,
            "admin_error": admin_error,
            "admin_message": admin_message,
        },
    )
@app.post("/admin/delete_problem", response_class=HTMLResponse)
async def admin_delete_problem(
    request: Request,
    admin_key: str = Form(...),
    problem_id: str = Form(...),
    workbook: str = Form(...),
):
    progress = None
    error = None
    admin_error = None
    admin_message = None

    # 1) 관리자 키 확인
    if admin_key != ADMIN_KEY:
        admin_error = "관리자 키가 올바르지 않습니다."
    else:
        # 2) 문제 번호 검증
        problem_id = problem_id.strip()
        if not problem_id.isdecimal():
            admin_error = "문제 번호는 1000 이상의 정수여야 합니다."
        else:
            pid_int = int(problem_id)
            if pid_int < 1000:
                admin_error = "문제 번호는 1000 이상의 정수여야 합니다."
            else:
                # 3) workbook 검증
                if workbook not in WORKBOOKS:
                    admin_error = "존재하지 않는 문제집입니다."
                else:
                    problems = WORKBOOKS[workbook]["problems"]
                    
                    if pid_int not in problems:
                        admin_error = f"해당 문제({pid_int})는 문제집에 존재하지 않습니다."
                    else:
                        problems.remove(pid_int)   # 삭제
                        save_workbooks_to_file()
                        admin_message = (
                            f"문제 {pid_int} 이(가) '{WORKBOOKS[workbook]['name']}' 문제집에서 삭제되었습니다."
                        )

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "workbooks": WORKBOOKS,
            "selected_handle": "",
            "selected_workbook": workbook if workbook in WORKBOOKS else "",
            "progress": progress,
            "error": error,
            "admin_error": admin_error,
            "admin_message": admin_message,
        },
    )
