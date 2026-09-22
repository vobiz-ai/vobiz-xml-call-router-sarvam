"""A stand-in backend, so you can see exactly what the router forwards.

Run it, point AI_ANSWER_URL at http://127.0.0.1:8091/answer, place a call,
and it prints every field the router sent.
"""
import json
from fastapi import FastAPI, Request
from fastapi.responses import Response

app = FastAPI()
seen = []


@app.post("/answer")
async def channel(request: Request):
    from urllib.parse import parse_qsl
    body = dict(parse_qsl((await request.body()).decode()))
    seen.append(body)
    print("\n=== what the backend receives ===")
    for k in sorted(body):
        print(f"  {k:<22} {body[k]}")
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
        '    <Speak>Mock backend speaking.</Speak>\n</Response>',
        media_type="application/xml")


@app.get("/seen")
async def get_seen():
    return {"seen": seen}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8091, log_level="warning")
