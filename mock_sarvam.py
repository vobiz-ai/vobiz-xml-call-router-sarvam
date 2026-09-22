"""A stand-in for Sarvam's answer URL, so we can see exactly what IPAC forwards."""
import json
from fastapi import FastAPI, Request
from fastapi.responses import Response

app = FastAPI()
seen = []


@app.post("/channels/vobiz")
async def channel(request: Request):
    from urllib.parse import parse_qsl
    body = dict(parse_qsl((await request.body()).decode()))
    seen.append(body)
    print("\n=== what Sarvam receives ===")
    for k in sorted(body):
        print(f"  {k:<22} {body[k]}")
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n'
        '    <Speak>Sarvam agent speaking.</Speak>\n</Response>',
        media_type="application/xml")


@app.get("/seen")
async def get_seen():
    return {"seen": seen}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8091, log_level="warning")
