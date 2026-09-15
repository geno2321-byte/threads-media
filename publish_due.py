"""깃허브가 5분마다 돌리는 예약 발행 시계. 이 파일은 예약 저장소로 그대로 올라간다.

깃허브가 5분마다 돌리는 예약 발행 시계. PC가 꺼져 있어도 여기서 글이 올라간다.

queue/<글번호>.json 에 예약 카드가 있다. 시각이 지난 카드를 스레드에 올리고
결과를 done/<글번호>.json 에 적는다. 앱은 켜질 때 done/ 을 읽어 발행 이력에 채운다.

같은 글이 두 번 올라가면 안 되므로, 올리기 전에 카드를 doing/ 으로 옮겨
먼저 밀어 넣는다(찜하기). 그 밀기가 실패하면 이번 판은 그냥 넘어간다.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://graph.threads.net/v1.0"

# 계정마다 열쇠가 따로다. 앱이 THREADS_TOKEN에 {계정번호: 열쇠} 묶음을 넣고, 카드에는 계정번호가 적혀 있다.
# 예전엔 열쇠 하나만 들어 있어 다른 계정 예약이 마지막에 연결한 계정으로 나갈 수 있었다(260915).
# 계정번호가 없는 옛 카드는 THREADS_USER_ID 계정으로 본다.
RAW = os.environ.get("THREADS_TOKEN", "")
try:
    KEYS = json.loads(RAW) if RAW.startswith("{") else {}
except ValueError:
    KEYS = {}

# 지금 올리는 카드의 계정. 카드마다 _use()가 바꿔 쥔다.
TOKEN = ""
USER_ID = ""

# 댓글을 여러 개 담을 때 앱이 쓰는 구분자. app.py·publisher.py의 것과 같아야 한다.
COMMENT_SEP = chr(30)

# 본문이 막 올라간 직후엔 스레드가 그 글을 아직 모른다며 댓글을 거절한다. 쉬었다 다시 단다.
# publisher.py의 것과 같게 둔다.
REPLY_WAITS = (0, 3, 8, 15, 30)

QUEUE = Path("queue")
DOING = Path("doing")
DONE = Path("done")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _call(path, params, method="GET"):
    body = urllib.parse.urlencode(dict(params, access_token=TOKEN)).encode()
    url = "%s/%s" % (API, path)
    if method == "GET":
        req = urllib.request.Request(url + "?" + body.decode())
    else:
        req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            pass
        raise RuntimeError("스레드가 거절했습니다 (%s): %s" % (e.code, detail))
    except Exception as e:
        raise RuntimeError("스레드에 닿지 못했습니다: %s" % e)


def _wait(container_id, kind):
    """스레드가 사진·영상을 받아가는 동안 기다린다. 영상은 오래 걸린다."""
    tries, gap = (60, 5) if kind == "video" else (15, 2)
    for _ in range(tries):
        got = _call(container_id, {"fields": "status,error_message"})
        if got.get("status") in ("FINISHED", "PUBLISHED"):
            return
        if got.get("status") in ("ERROR", "EXPIRED"):
            raise RuntimeError(got.get("error_message") or "스레드가 파일을 받지 못했습니다.")
        time.sleep(gap)
    raise RuntimeError("스레드가 파일을 받는 데 너무 오래 걸립니다.")


def _media_params(url, kind):
    if kind == "video":
        return {"media_type": "VIDEO", "video_url": url}
    return {"media_type": "IMAGE", "image_url": url}


def publish(text, media, reply_to_id=None):
    """글 하나를 올리고 글번호를 돌려준다. media는 (주소, 종류) 목록이다."""
    if len(media) == 1:
        params = dict(_media_params(*media[0]), text=text)
    elif media:
        children = []
        for url, kind in media:
            child = _call("%s/threads" % USER_ID,
                          dict(_media_params(url, kind), is_carousel_item="true"), "POST")["id"]
            _wait(child, kind)
            children.append(child)
        params = {"media_type": "CAROUSEL", "children": ",".join(children), "text": text}
    else:
        params = {"media_type": "TEXT", "text": text}
    if reply_to_id:
        params["reply_to_id"] = reply_to_id

    creation_id = _call("%s/threads" % USER_ID, params, "POST")["id"]
    if media:
        _wait(creation_id, "video" if any(k == "video" for _, k in media) else "image")
    return _call("%s/threads_publish" % USER_ID, {"creation_id": creation_id}, "POST")["id"]


def _use(job):
    """이 카드를 쓴 계정의 열쇠로 바꿔 쥔다."""
    global TOKEN, USER_ID
    USER_ID = str(job.get("user_id") or os.environ.get("THREADS_USER_ID", ""))
    if KEYS:
        TOKEN = KEYS.get(USER_ID, "")
    else:
        # 열쇠가 하나뿐인 옛 비밀값. 그 열쇠 주인 계정의 카드일 때만 쓴다.
        TOKEN = RAW if USER_ID == os.environ.get("THREADS_USER_ID", "") else ""


def _reply(text, reply_to_id):
    for wait in REPLY_WAITS:
        time.sleep(wait)
        try:
            return publish(text, [], reply_to_id=reply_to_id)
        except RuntimeError as e:
            error = e
            # 연결이 끊긴 거면 올라갔는지 모른다. 두 번 달리지 않게 여기서 멈춘다.
            if str(e).startswith("스레드에 닿지 못했습니다"):
                break
    raise error


def _permalink(thread_id):
    try:
        return _call(thread_id, {"fields": "permalink"}).get("permalink")
    except RuntimeError:
        return None


def send(job):
    """카드 하나를 올린다. 예외를 던지지 않고 결과를 돌려준다."""
    _use(job)
    if USER_ID and not TOKEN:
        return {"status": "실패", "thread_id": None, "permalink": None,
                "error": "이 계정(%s)의 열쇠가 깃허브에 없습니다. 앱 설정에서 [기한 늘리기]를 한 번 눌러 주세요."
                         % USER_ID}

    media = [(f["url"], f["kind"]) for f in job.get("files", [])]
    try:
        thread_id = publish(job["text"], media)
    except RuntimeError as e:
        return {"status": "실패", "thread_id": None, "permalink": None, "error": str(e)}

    # 댓글이 여러 개면 앞 댓글에 이어 단다. 하나뿐인 옛 카드도 그대로 돈다.
    # 막히면 어디까지 달았는지 적어 둔다. 앱의 [댓글 다시 달기]가 거기서부터 잇는다.
    last_id, done = thread_id, 0
    for one in (job.get("comment") or "").split(COMMENT_SEP):
        if not one.strip():
            continue
        try:
            last_id = _reply(one, last_id)
        except RuntimeError as e:
            return {"status": "본문만 발행", "thread_id": thread_id, "permalink": _permalink(thread_id),
                    "error": str(e), "reply_to": last_id, "replied": done}
        done += 1

    return {"status": "발행완료", "thread_id": thread_id, "permalink": _permalink(thread_id),
            "error": None}


def git(*args, check=True):
    return subprocess.run(("git",) + args, check=check, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def push(message):
    """바꾼 것을 올린다. 올리지 못하면 False."""
    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        return True
    git("commit", "-m", message)
    for _ in range(3):
        git("pull", "--rebase", check=False)
        if git("push", check=False).returncode == 0:
            return True
        time.sleep(3)
    return False


def claim(paths):
    """올리기 전에 카드를 doing/ 으로 옮겨 먼저 저장소에 밀어 넣는다."""
    DOING.mkdir(exist_ok=True)
    moved = []
    for p in paths:
        dest = DOING / p.name
        p.replace(dest)
        moved.append(dest)
    if not push("예약 글 %d개 가져감" % len(moved)):
        print("찜하기를 밀지 못했습니다. 이번 판은 넘어갑니다.")
        return []
    return moved


def main():
    if not RAW:
        sys.exit("THREADS_TOKEN 비밀값이 없습니다.")

    due = []
    for path in sorted(QUEUE.glob("*.json")):
        job = json.loads(path.read_text(encoding="utf-8"))
        if job.get("at_utc", "") <= now():
            due.append(path)
    if not due:
        print("올릴 예약이 없습니다.")
        return

    git("config", "user.name", "threads-scheduler")
    git("config", "user.email", "scheduler@users.noreply.github.com")

    DONE.mkdir(exist_ok=True)
    for path in claim(due):
        job = json.loads(path.read_text(encoding="utf-8"))
        result = send(job) if not job.get("dry") else {
            "status": "연습", "thread_id": None, "permalink": None, "error": None}
        result["id"] = job["id"]
        result["done_at"] = now()
        (DONE / path.name).write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        path.unlink()
        for used in job.get("files", []):
            Path(used["path"]).unlink(missing_ok=True)
        print("%s → %s %s" % (job["id"], result["status"], result["error"] or ""))

    if not push("예약 발행 결과"):
        print("결과를 밀지 못했습니다. 다음 판에서 다시 밉니다.")


if __name__ == "__main__":
    main()
