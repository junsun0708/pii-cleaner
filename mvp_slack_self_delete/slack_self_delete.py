"""
지정한 Slack 채널/DM 의 본인 메시지만 삭제.

흐름:
  auth.test → my_user_id
  conversations.history (+ thread replies) → user == my_user_id 인 메시지 후보
  dry-run: 출력 / execute: chat.delete + 로그

제한:
  - User OAuth Token 필요 (`xoxp-...`)
  - 본인 메시지만 (Slack 정책)
  - rate limit Tier 3 = 50+ req/min, chat.delete 는 conservatively 1 req/sec
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

THIS_DIR = Path(__file__).resolve().parent
LOG_DIR = THIS_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
BACKUP_DIR = THIS_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("slack_self_delete")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="지정 채널/DM 의 본인 메시지만 삭제",
        epilog=(
            "예시:\n"
            "  --channel C0123ABCD       # 채널 ID 직접\n"
            "  --user U0987XYZ           # 그 사람과의 1:1 DM 자동 조회\n"
            "  --user @hong.gildong      # 핸들로 조회\n"
            "  --user hong@example.com   # 이메일로 조회"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--channel", help="채널/DM ID (C/G/D/MP... 로 시작)")
    target.add_argument("--user", help="DM 상대방. user_id(U...) / @핸들 / 이메일 모두 가능")
    p.add_argument("--dry-run", action="store_true", help="삭제 대상 미리보기 (실제 삭제 X) [기본]")
    p.add_argument("--execute", action="store_true", help="실제 삭제 실행")
    p.add_argument("--backup", action="store_true", help="대상 채널/DM 전체 (본인+상대방+스레드+첨부 메타) JSON 백업")
    p.add_argument("--backup-only", action="store_true", help="백업만 하고 종료 (삭제 X)")
    p.add_argument("--delete-files", action="store_true", help="본인 메시지의 첨부 파일도 함께 삭제 (files:write scope 필요)")
    p.add_argument("--workers", type=int, default=1, help="병렬 삭제 worker 수 (기본 1). 권장 1~5. rate limit (50 rpm) 에 막혀 5 초과는 의미 없음")
    p.add_argument("--since", type=str, default=None, help="YYYY-MM-DD 이후 메시지만")
    p.add_argument("--until", type=str, default=None, help="YYYY-MM-DD 이전 메시지만")
    p.add_argument("--limit", type=int, default=None, help="최대 N개만 처리 (테스트용)")
    p.add_argument("--keep-pattern", type=str, default=None, help="이 문자열 포함하는 메시지는 제외")
    p.add_argument("--sleep", type=float, default=1.1, help="chat.delete 간격(초). 기본 1.1")
    args = p.parse_args()
    if args.execute and args.dry_run:
        p.error("--execute 와 --dry-run 동시 사용 불가")
    if not args.execute:
        args.dry_run = True
    return args


def resolve_user_id(client: WebClient, hint: str) -> str:
    """user_id(U...) / @핸들 / 이메일 → user_id."""
    hint = hint.strip()

    # 1) user_id 직접
    if hint.startswith(("U", "W")) and len(hint) > 5 and " " not in hint:
        return hint

    # 2) 이메일
    if "@" in hint and "." in hint.split("@")[-1]:
        resp = client.users_lookupByEmail(email=hint)
        if not resp.get("ok"):
            raise RuntimeError(f"users.lookupByEmail fail: {resp.get('error')}")
        return resp["user"]["id"]

    # 3) @핸들 또는 display name → users.list 순회
    target_handle = hint.lstrip("@").lower()
    cursor: str | None = None
    while True:
        kwargs = {"limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.users_list(**kwargs)
        if not resp.get("ok"):
            raise RuntimeError(f"users.list fail: {resp.get('error')}")
        for u in resp.get("members", []):
            if u.get("deleted") or u.get("is_bot"):
                continue
            names = {
                (u.get("name") or "").lower(),
                (u.get("real_name") or "").lower(),
                ((u.get("profile") or {}).get("display_name") or "").lower(),
                ((u.get("profile") or {}).get("real_name") or "").lower(),
            }
            if target_handle in names:
                return u["id"]
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    raise RuntimeError(f"사용자 못 찾음: {hint!r}")


def open_dm(client: WebClient, other_user_id: str) -> str:
    """그 사람과의 1:1 DM channel id 반환 (없으면 생성)."""
    resp = client.conversations_open(users=other_user_id)
    if not resp.get("ok"):
        raise RuntimeError(f"conversations.open fail: {resp.get('error')}")
    return resp["channel"]["id"]


def backup_conversation(client: WebClient, channel: str, since_ts: float | None, until_ts: float | None) -> Path:
    """채널/DM 의 모든 메시지 (본인+상대방+스레드) JSON 백업."""
    all_msgs: list[dict] = []
    cursor: str | None = None

    while True:
        kwargs = {"channel": channel, "limit": 200}
        if cursor: kwargs["cursor"] = cursor
        if since_ts: kwargs["oldest"] = str(since_ts)
        if until_ts: kwargs["latest"] = str(until_ts)

        resp = client.conversations_history(**kwargs)
        if not resp.get("ok"):
            log.error("backup history fail: %s", resp.get("error"))
            break

        for msg in resp.get("messages", []):
            all_msgs.append(msg)
            # 스레드 회신
            if msg.get("thread_ts") and msg.get("reply_count", 0) > 0:
                rcur: str | None = None
                while True:
                    rkw = {"channel": channel, "ts": msg["thread_ts"], "limit": 200}
                    if rcur: rkw["cursor"] = rcur
                    rresp = client.conversations_replies(**rkw)
                    if not rresp.get("ok"): break
                    for r in rresp.get("messages", []):
                        if r.get("ts") != msg["thread_ts"]:  # parent 중복 제거
                            all_msgs.append(r)
                    if not rresp.get("has_more"): break
                    rcur = (rresp.get("response_metadata") or {}).get("next_cursor")
                    if not rcur: break
                    time.sleep(0.5)

        if not resp.get("has_more"): break
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor: break
        time.sleep(0.5)

    # 시간순 정렬
    all_msgs.sort(key=lambda m: float(m.get("ts", 0)))

    # 등장 user_id 집합 → 사용자 메타 함께 저장 (이름/이메일 매핑)
    user_ids = {m.get("user") for m in all_msgs if m.get("user")}
    users: dict[str, dict] = {}
    for uid in user_ids:
        try:
            u = client.users_info(user=uid)
            if u.get("ok"):
                p = u["user"]
                users[uid] = {
                    "id": uid,
                    "name": p.get("name"),
                    "real_name": p.get("real_name"),
                    "email": (p.get("profile") or {}).get("email"),
                }
        except SlackApiError:
            users[uid] = {"id": uid, "name": "?"}
        time.sleep(0.2)

    # 저장 — JSON (전체) + TXT (사람 읽기 쉽게)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = BACKUP_DIR / f"backup_{channel}_{stamp}.json"
    txt_path = BACKUP_DIR / f"backup_{channel}_{stamp}.txt"

    # JSON — 전체 메타 보존
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "channel": channel,
            "exported_at": datetime.now().isoformat(),
            "since": datetime.fromtimestamp(since_ts).isoformat() if since_ts else None,
            "until": datetime.fromtimestamp(until_ts).isoformat() if until_ts else None,
            "message_count": len(all_msgs),
            "users": users,
            "messages": all_msgs,
        }, f, ensure_ascii=False, indent=2)

    # TXT — [시간] 이름: 텍스트
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"# Slack 대화 백업\n")
        f.write(f"# 채널: {channel}\n")
        f.write(f"# 백업 시각: {datetime.now().isoformat()}\n")
        f.write(f"# 메시지 수: {len(all_msgs)}\n")
        f.write(f"#\n")
        for m in all_msgs:
            uid = m.get("user") or m.get("bot_id") or "?"
            who = users.get(uid, {}).get("real_name") or users.get(uid, {}).get("name") or uid
            dt = datetime.fromtimestamp(float(m["ts"])).strftime("%Y-%m-%d %H:%M:%S")
            text = (m.get("text") or "").replace("\n", " ").strip()
            # 스레드 회신은 indent
            prefix = "  └ " if m.get("thread_ts") and m.get("thread_ts") != m.get("ts") else ""
            f.write(f"[{dt}] {prefix}{who}: {text}\n")

    log.info("백업 저장: %s + %s (%d 메시지, %d 사용자)", json_path.name, txt_path.name, len(all_msgs), len(users))
    return json_path


def to_ts(date_str: str | None) -> float | None:
    if not date_str:
        return None
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return dt.timestamp()


def collect_my_messages(
    client: WebClient,
    channel: str,
    my_user_id: str,
    since_ts: float | None,
    until_ts: float | None,
    keep_pattern: str | None,
    limit: int | None,
) -> list[dict]:
    """채널 + 스레드 회신 합쳐서 본인 메시지만 모음."""
    out: list[dict] = []
    cursor: str | None = None

    while True:
        kwargs = {"channel": channel, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        if since_ts:
            kwargs["oldest"] = str(since_ts)
        if until_ts:
            kwargs["latest"] = str(until_ts)

        resp = client.conversations_history(**kwargs)
        if not resp.get("ok"):
            log.error("conversations.history fail: %s", resp.get("error"))
            break

        for msg in resp.get("messages", []):
            if _is_mine(msg, my_user_id, keep_pattern):
                out.append(msg)
                if limit and len(out) >= limit:
                    return out
            # 스레드 회신 (parent 메시지가 thread_ts 보유)
            if msg.get("thread_ts") and msg.get("reply_count", 0) > 0:
                out.extend(_collect_thread(client, channel, msg["thread_ts"], my_user_id, keep_pattern, limit, len(out)))
                if limit and len(out) >= limit:
                    return out[:limit]

        if not resp.get("has_more"):
            break
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(1)  # history Tier 3 rate

    return out


def _collect_thread(
    client: WebClient,
    channel: str,
    thread_ts: str,
    my_user_id: str,
    keep_pattern: str | None,
    limit: int | None,
    already: int,
) -> list[dict]:
    out: list[dict] = []
    cursor: str | None = None
    while True:
        kwargs = {"channel": channel, "ts": thread_ts, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.conversations_replies(**kwargs)
        if not resp.get("ok"):
            log.warning("conversations.replies fail thread=%s: %s", thread_ts, resp.get("error"))
            break
        for msg in resp.get("messages", []):
            # parent 메시지는 history 에서 이미 처리되므로 중복 방지
            if msg.get("ts") == thread_ts:
                continue
            if _is_mine(msg, my_user_id, keep_pattern):
                out.append(msg)
                if limit and already + len(out) >= limit:
                    return out
        if not resp.get("has_more"):
            break
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(1)
    return out


def _is_mine(msg: dict, my_user_id: str, keep_pattern: str | None) -> bool:
    if msg.get("user") != my_user_id:
        return False
    if msg.get("subtype") in ("bot_message", "channel_join", "channel_leave"):
        return False
    if keep_pattern and keep_pattern in (msg.get("text") or ""):
        return False
    return True


def _delete_files_of_msg(client: WebClient, msg: dict, my_user_id: str) -> dict:
    """메시지의 첨부 파일을 삭제. 본인 소유 파일만."""
    out = {"ok": 0, "skip": 0, "fail": 0}
    for fobj in (msg.get("files") or []):
        fid = fobj.get("id")
        if not fid:
            continue
        # 파일 owner 가 본인인지 재검증 (다른 사람 공유 파일 보호)
        f_user = fobj.get("user")
        if f_user and f_user != my_user_id:
            log.info("    file=%s skip (owner=%s != me)", fid, f_user)
            out["skip"] += 1
            continue
        try:
            resp = client.files_delete(file=fid)
            if resp.get("ok"):
                log.info("    file=%s OK (%s)", fid, fobj.get("name") or "")
                out["ok"] += 1
            else:
                log.warning("    file=%s FAIL %s", fid, resp.get("error"))
                out["fail"] += 1
        except SlackApiError as e:
            err = e.response.get("error", "")
            if err in ("file_not_found", "file_deleted"):
                out["skip"] += 1
            else:
                log.warning("    file=%s ERR %s", fid, err)
                out["fail"] += 1
        time.sleep(0.5)
    return out


def _delete_one(
    client: WebClient,
    channel: str,
    msg: dict,
    my_user_id: str,
    delete_files: bool,
    sleep_sec: float,
    log_lock: threading.Lock,
    log_fp,
) -> dict:
    """단일 메시지 삭제. ratelimited 시 자동 retry. 결과 dict."""
    result = {"ok": 0, "skip": 0, "fail": 0, "file_ok": 0, "file_skip": 0, "file_fail": 0}
    if msg.get("user") != my_user_id:
        result["skip"] += 1
        return result
    ts = msg["ts"]
    text_head = (msg.get("text") or "")[:100]

    if delete_files and msg.get("files"):
        fc = _delete_files_of_msg(client, msg, my_user_id)
        result["file_ok"] = fc["ok"]; result["file_skip"] = fc["skip"]; result["file_fail"] = fc["fail"]

    # 최대 5회 retry (ratelimited)
    for attempt in range(5):
        try:
            resp = client.chat_delete(channel=channel, ts=ts)
            if resp.get("ok"):
                result["ok"] += 1
                with log_lock:
                    log.info("OK ts=%s %r", ts, text_head)
                    log_fp.write(json.dumps({
                        "ok": True, "ts": ts, "text_head": text_head,
                        "deleted_at": datetime.now().isoformat(),
                    }, ensure_ascii=False) + "\n")
                    log_fp.flush()
            else:
                with log_lock:
                    log.warning("FAIL ts=%s error=%s", ts, resp.get("error"))
                result["fail"] += 1
            break
        except SlackApiError as e:
            err = e.response.get("error", "")
            if err == "ratelimited":
                retry_after = int(e.response.headers.get("Retry-After", "5"))
                with log_lock:
                    log.warning("rate limit ts=%s; sleep %ds (attempt %d/5)", ts, retry_after, attempt+1)
                time.sleep(retry_after + 1)
                continue
            if err in ("message_not_found", "cant_delete_message"):
                with log_lock:
                    log.info("SKIP ts=%s reason=%s", ts, err)
                result["skip"] += 1
                break
            with log_lock:
                log.error("ERR ts=%s err=%s", ts, err)
            result["fail"] += 1
            break

    time.sleep(sleep_sec)
    return result


def delete_messages(
    client: WebClient,
    channel: str,
    messages: list[dict],
    my_user_id: str,
    sleep_sec: float,
    delete_files: bool = False,
    workers: int = 1,
) -> dict:
    """본인 메시지 삭제 — 옵션: 병렬 worker + 자동 rate-limit retry."""
    log_path = LOG_DIR / f"deleted_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    counts = {"ok": 0, "skip": 0, "fail": 0, "file_ok": 0, "file_skip": 0, "file_fail": 0}
    lock = threading.Lock()

    # rate limit 분산: workers 가 N개면 각자 sleep 을 N배. 글로벌 RPS 유지.
    per_worker_sleep = sleep_sec * workers if workers > 1 else sleep_sec

    log.info("삭제 시작: %d 메시지, workers=%d, per-worker sleep=%.2fs", len(messages), workers, per_worker_sleep)

    with open(log_path, "w", encoding="utf-8") as f:
        if workers <= 1:
            # 직렬 — 진행률 표시 깔끔
            for i, msg in enumerate(messages, 1):
                r = _delete_one(client, channel, msg, my_user_id, delete_files, per_worker_sleep, lock, f)
                for k, v in r.items():
                    counts[k] += v
                if i % 10 == 0 or i == len(messages):
                    log.info("진행: %d/%d", i, len(messages))
        else:
            # 병렬 — ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [
                    ex.submit(_delete_one, client, channel, m, my_user_id, delete_files, per_worker_sleep, lock, f)
                    for m in messages
                ]
                done = 0
                for fut in as_completed(futures):
                    r = fut.result()
                    for k, v in r.items():
                        counts[k] += v
                    done += 1
                    if done % 10 == 0 or done == len(messages):
                        with lock:
                            log.info("진행: %d/%d", done, len(messages))

    log.info("로그 저장: %s", log_path)
    return counts


def main():
    args = parse_args()
    load_dotenv(THIS_DIR / ".env")
    token = os.environ.get("SLACK_USER_TOKEN")
    if not token or not token.startswith("xoxp-"):
        log.error("SLACK_USER_TOKEN (xoxp-...) 가 .env 에 없음")
        sys.exit(1)

    client = WebClient(token=token)
    me = client.auth_test()
    if not me.get("ok"):
        log.error("auth.test fail: %s", me.get("error"))
        sys.exit(1)
    my_user_id = me["user_id"]
    log.info("로그인: user=%s (%s) team=%s", me.get("user"), my_user_id, me.get("team"))

    # 대상 채널 결정 — --channel 또는 --user(상대방 → DM 자동 조회)
    if args.user:
        try:
            target_user_id = resolve_user_id(client, args.user)
        except RuntimeError as e:
            log.error("사용자 해석 실패: %s", e)
            sys.exit(1)
        if target_user_id == my_user_id:
            log.error("--user 가 본인입니다. 본인 DM 은 의미 없음.")
            sys.exit(1)
        channel_id = open_dm(client, target_user_id)
        log.info("DM 상대: %s (%s) → channel=%s", args.user, target_user_id, channel_id)
    else:
        channel_id = args.channel

    # 채널 메타 정보 — 표시용이라 실패해도 진행 (im:read/channels:read scope 없을 때 우회)
    try:
        info = client.conversations_info(channel=channel_id)
        ch_name = (info.get("channel") or {}).get("name") or "(DM)"
    except SlackApiError as e:
        if e.response.get("error") == "missing_scope":
            log.warning("conversations.info scope 없음 — 채널명 표시 skip (계속 진행)")
            ch_name = "(scope 부족)"
        else:
            log.error("채널 접근 불가: %s", e.response.get("error"))
            sys.exit(1)
    log.info("대상 채널: %s [%s]", channel_id, ch_name)

    since_ts = to_ts(args.since)
    until_ts = to_ts(args.until)
    log.info("범위: since=%s until=%s limit=%s keep_pattern=%r",
             args.since, args.until, args.limit, args.keep_pattern)

    # 0단계: 백업 (옵션)
    if args.backup or args.backup_only:
        log.info("=== 0단계: 전체 백업 (본인+상대방+스레드) ===")
        backup_conversation(client, channel_id, since_ts, until_ts)
        if args.backup_only:
            log.info("--backup-only 종료 (삭제 X)")
            return

    log.info("=== 1단계: 본인 메시지 수집 ===")
    messages = collect_my_messages(
        client, channel_id, my_user_id,
        since_ts, until_ts, args.keep_pattern, args.limit,
    )
    log.info("수집 완료: %d 개", len(messages))

    if not messages:
        log.info("삭제 대상 없음.")
        return

    log.info("=== 미리보기 (최근 5개) ===")
    file_total = sum(len(m.get("files") or []) for m in messages)
    for m in messages[:5]:
        text = (m.get("text") or "")[:80]
        dt = datetime.fromtimestamp(float(m["ts"]))
        n_files = len(m.get("files") or [])
        suffix = f"  📎×{n_files}" if n_files else ""
        log.info("  %s  %r%s", dt.strftime("%Y-%m-%d %H:%M"), text, suffix)
    if len(messages) > 5:
        log.info("  ... (총 %d 개, 첨부 파일 %d 개)", len(messages), file_total)
    if args.delete_files and file_total:
        log.info("  ⚠ --delete-files 켜져있어 첨부 %d 개도 함께 삭제 예정", file_total)

    if args.dry_run:
        log.info("=== DRY-RUN 종료 (--execute 추가 시 실제 삭제) ===")
        return

    log.info("=== 2단계: 삭제 실행 ===")
    counts = delete_messages(
        client, channel_id, messages, my_user_id,
        args.sleep, args.delete_files, args.workers,
    )
    log.info("완료: %s", counts)


if __name__ == "__main__":
    main()
