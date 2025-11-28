# 서버 실행 : py -m uvicorn main:app --reload
#
import json
from pathlib import Path
from typing import Dict, Any, List, Set, Optional

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import httpx

from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "workbooks.json", "r", encoding="utf-8") as f:
    WORKBOOKS: Dict[str, Any] = json.load(f)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="BOJ Workbook Progress Viewer")

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


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

    async def close(self):
        await self.client.aclose()


solvedac_client = SolvedAcClient()


@app.on_event("shutdown")
async def shutdown_event():
    await solvedac_client.close()


def compute_progress(handle: str, workbook_key: str, solved_set: Set[int]):
    wb = WORKBOOKS[workbook_key]
    problems = wb["problems"]

    solved_in = []
    unsolved_in = []

    for pid in problems:
        if pid in solved_set:
            solved_in.append(pid)
        else:
            unsolved_in.append(pid)

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
        },
    )
