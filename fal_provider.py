"""fal.ai video generation provider (queue REST API, plain requests).

Flow: POST job -> get request_id -> poll status -> download result.
Docs: https://docs.fal.ai/model-endpoints/queue
"""
import time
import requests

QUEUE_BASE = "https://queue.fal.run"


class FalError(Exception):
    pass


def _headers(fal_key: str) -> dict:
    return {"Authorization": f"Key {fal_key}", "Content-Type": "application/json"}


def submit_job(fal_key: str, model_id: str, prompt: str,
               duration: str = "5", aspect_ratio: str = "9:16",
               image_url: str | None = None) -> str:
    """Submit a generation job, return the request_id."""
    payload = {
        "prompt": prompt,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
    }
    if image_url:
        payload["image_url"] = image_url  # image-to-video endpoints use this

    r = requests.post(f"{QUEUE_BASE}/{model_id}", json=payload,
                      headers=_headers(fal_key), timeout=60)
    if r.status_code not in (200, 201, 202):
        raise FalError(f"submit failed [{r.status_code}]: {r.text[:300]}")
    data = r.json()
    request_id = data.get("request_id")
    if not request_id:
        raise FalError(f"no request_id in response: {data}")
    return request_id


def poll_job(fal_key: str, model_id: str, request_id: str,
             timeout_s: int = 900, interval_s: int = 10) -> str:
    """Poll until done, return the video URL."""
    status_url = f"{QUEUE_BASE}/{model_id}/requests/{request_id}/status"
    result_url = f"{QUEUE_BASE}/{model_id}/requests/{request_id}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(status_url, headers=_headers(fal_key), timeout=30)
        if r.status_code == 200:
            st = r.json().get("status")
            if st == "COMPLETED":
                break
            if st in ("FAILED", "CANCELLED"):
                raise FalError(f"job {st.lower()}")
        time.sleep(interval_s)
    else:
        raise FalError("timed out waiting for generation")

    r = requests.get(result_url, headers=_headers(fal_key), timeout=60)
    if r.status_code != 200:
        raise FalError(f"result fetch failed [{r.status_code}]: {r.text[:300]}")
    data = r.json()
    # kling/veo style: {"video": {"url": ...}} ; fall back to common shapes
    url = (data.get("video") or {}).get("url") \
        or (data.get("videos") or [{}])[0].get("url") \
        or data.get("url")
    if not url:
        raise FalError(f"no video url in result: {str(data)[:300]}")
    return url


def download(url: str, out_path: str) -> str:
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return out_path


def upload_image_to_fal(fal_key: str, file_path: str) -> str:
    """Upload a local image to fal storage, get a public URL for i2v."""
    # fal supports direct file upload via the files endpoint
    r = requests.post(
        "https://rest.alpha.fal.ai/storage/upload",
        headers={"Authorization": f"Key {fal_key}"},
        files={"file": open(file_path, "rb")},
        timeout=120,
    )
    if r.status_code not in (200, 201):
        raise FalError(f"image upload failed [{r.status_code}]: {r.text[:300]}")
    data = r.json()
    url = data.get("url") or data.get("file_url")
    if not url:
        raise FalError(f"no url in upload response: {data}")
    return url


def generate_video(fal_key: str, model_id: str, prompt: str, out_path: str,
                   duration: str = "5", aspect_ratio: str = "9:16",
                   image_url: str | None = None,
                   progress_cb=None) -> str:
    """Full pipeline: submit -> poll -> download. Returns out_path."""
    if progress_cb:
        progress_cb("Submitting job…")
    rid = submit_job(fal_key, model_id, prompt, duration, aspect_ratio, image_url)
    if progress_cb:
        progress_cb("Generating (this takes 2–8 minutes)…")
    url = poll_job(fal_key, model_id, rid)
    if progress_cb:
        progress_cb("Downloading your video…")
    return download(url, out_path)
