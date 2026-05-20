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
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

THIS_DIR = Path(__file__).resolve().parent
LOG_DIR = THIS_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("slack_self_delete")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="지정 채널/DM 의 본인 메시지만 삭제")
    p.add_argument("--channel", required=True, help="채널 ID (C/G/D/MP... 로 시작)")
    p.add_argument("--dry-run", action="store_true", help="삭제 대상 미리보기 (실제 삭제 X) [기본]")
    p.add_argument("--execute", action="store_true", help="실제 삭제 실행")
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


def delete_messages(client: WebClient, channel: str, messages: list[dict], my_user_id: str, sleep_sec: float) -> dict:
    """본인 메시지 삭제. user_id 재검증 + rate limit + 로그."""
    log_path = LOG_DIR / f"deleted_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
    counts = {"ok": 0, "skip": 0, "fail": 0}

    with open(log_path, "w", encoding="utf-8") as f:
        for i, msg in enumerate(messages, 1):
            # 재검증 — 절대 다른 사람 메시지 삭제하지 않음
            if msg.get("user") != my_user_id:
                log.warning("[%d] SKIP — user_id 불일치 (%s != %s)", i, msg.get("user"), my_user_id)
                counts["skip"] += 1
                continue
            ts = msg["ts"]
            text_head = (msg.get("text") or "")[:100]
            try:
                resp = client.chat_delete(channel=channel, ts=ts)
                if resp.get("ok"):
                    log.info("[%d/%d] OK ts=%s text=%r", i, len(messages), ts, text_head)
                    counts["ok"] += 1
                    f.write(json.dumps({
                        "ok": True, "ts": ts, "text_head": text_head,
                        "deleted_at": datetime.now().isoformat(),
                    }, ensure_ascii=False) + "\n")
                else:
                    log.warning("[%d] FAIL ts=%s error=%s", i, ts, resp.get("error"))
                    counts["fail"] += 1
            except SlackApiError as e:
                err = e.response.get("error", "")
                if err == "ratelimited":
                    retry_after = int(e.response.headers.get("Retry-After", "5"))
                    log.warning("rate limit; sleep %d", retry_after)
                    time.sleep(retry_after + 1)
                    continue  # 같은 메시지 재시도 위해 i 증가 X — 단순화 위해 다음으로
                if err in ("message_not_found", "cant_delete_message"):
                    log.info("[%d] SKIP ts=%s reason=%s", i, ts, err)
                    counts["skip"] += 1
                else:
                    log.error("[%d] ERR ts=%s err=%s", i, ts, err)
                    counts["fail"] += 1
            time.sleep(sleep_sec)

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

    info = client.conversations_info(channel=args.channel)
    if not info.get("ok"):
        log.error("채널 접근 불가: %s", info.get("error"))
        sys.exit(1)
    ch_name = (info.get("channel") or {}).get("name") or "(DM)"
    log.info("대상 채널: %s [%s]", args.channel, ch_name)

    since_ts = to_ts(args.since)
    until_ts = to_ts(args.until)
    log.info("범위: since=%s until=%s limit=%s keep_pattern=%r",
             args.since, args.until, args.limit, args.keep_pattern)

    log.info("=== 1단계: 본인 메시지 수집 ===")
    messages = collect_my_messages(
        client, args.channel, my_user_id,
        since_ts, until_ts, args.keep_pattern, args.limit,
    )
    log.info("수집 완료: %d 개", len(messages))

    if not messages:
        log.info("삭제 대상 없음.")
        return

    log.info("=== 미리보기 (최근 5개) ===")
    for m in messages[:5]:
        text = (m.get("text") or "")[:80]
        dt = datetime.fromtimestamp(float(m["ts"]))
        log.info("  %s  %r", dt.strftime("%Y-%m-%d %H:%M"), text)
    if len(messages) > 5:
        log.info("  ... (총 %d 개)", len(messages))

    if args.dry_run:
        log.info("=== DRY-RUN 종료 (--execute 추가 시 실제 삭제) ===")
        return

    log.info("=== 2단계: 삭제 실행 ===")
    counts = delete_messages(client, args.channel, messages, my_user_id, args.sleep)
    log.info("완료: %s", counts)


if __name__ == "__main__":
    main()
