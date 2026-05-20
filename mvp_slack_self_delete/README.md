# mvp_slack_self_delete

지정한 Slack 채널/DM 안에서 **본인이 작성한 메시지만** 삭제하는 1-task MVP.

## 한정 범위
- ✅ 본인 (User OAuth Token 소유자) 의 메시지만
- ✅ 채널 (public/private) + DM (1:1) + Multi-DM
- ✅ 스레드 회신 포함
- ❌ 다른 사람 메시지는 절대 안 건드림 (Slack 정책상 불가, 코드로도 차단)
- ❌ 첨부 파일 별도 삭제 안 함 (메시지 삭제 시 자동 unfurl 해제, 파일 자체 보존)

## 필요 권한 (User Token Scopes)
| Scope | 용도 |
|---|---|
| `channels:history` | public 채널 메시지 조회 |
| `groups:history` | private 채널 메시지 조회 |
| `im:history` | DM 메시지 조회 |
| `mpim:history` | Multi-DM 메시지 조회 |
| `chat:write` | 본인 메시지 삭제 |
| `users:read` (옵션) | 사용자 id 확인용 |

## 토큰 발급
1. `https://api.slack.com/apps` → 「Create New App」 (From scratch)
2. `OAuth & Permissions` → 위 6개 scope 추가 (**User Token Scopes** 영역)
3. 「Install to Workspace」 → 워크스페이스 관리자 승인
4. `User OAuth Token` (`xoxp-...`) 복사 → `.env` 에 `SLACK_USER_TOKEN=xoxp-...`

## 사용

```bash
cp .env.example .env
# .env 에 SLACK_USER_TOKEN 채움

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1) dry-run — 삭제 대상 미리보기 (실제 삭제 X)
python slack_self_delete.py --channel C0123ABCD --dry-run

# 2) 실행
python slack_self_delete.py --channel C0123ABCD

# DM 도 동일 (DM 의 channel id 도 C 또는 D 로 시작)
python slack_self_delete.py --channel D0987XYZ --dry-run

# 옵션
--since 2024-01-01      # 그 이후 메시지만
--until 2024-12-31      # 그 이전 메시지만
--limit 100             # 최대 N개만 (테스트용)
--keep-pattern "공지"   # 이 단어 포함한 본인 메시지는 제외
```

## 안전장치
1. **dry-run 기본 권장** — `--execute` 명시 안 하면 dry-run
2. **user_id 일치 확인** — 매 메시지마다 `msg.user == auth.test().user_id` 검증
3. **rate limit** — Slack 정책 = 1 req/sec (`chat.delete` Tier 3). `time.sleep(1.1)` 보수적
4. **로그** — 삭제한 메시지 ts/내용 첫 100자 → `logs/deleted_YYYYMMDD_HHMM.jsonl`
5. **중단 가능** — Ctrl+C 시 진행 상황 저장, 재시작 시 이어서

## 흐름
```
1. SLACK_USER_TOKEN 로드
2. auth.test → my_user_id 확인
3. conversations.info(channel) → 채널 존재·접근 가능 확인
4. conversations.history(channel, oldest, latest) 페이지 순회
   ├ 메시지마다:
   │  ├ user == my_user_id 면 후보에 추가
   │  └ thread_ts 있으면 conversations.replies 로 회신도 순회
   └ next_cursor 있으면 다음 페이지
5. dry-run 모드: 후보 목록 출력 + 개수
6. execute 모드:
   ├ chat.delete(channel, ts) 호출
   ├ 응답 ok 확인
   ├ 로그 파일에 기록
   └ sleep 1.1s
7. 종료 요약 (성공/실패/skip 개수)
```

## 에러 처리
- `ratelimited` → `Retry-After` 헤더 따라 대기 후 재시도
- `message_not_found` → 이미 삭제된 메시지, skip
- `cant_delete_message` → 채널 정책 또는 권한 부족, skip + 로그
- `invalid_auth` → 토큰 만료, 종료

## 제한사항
- **DM 상대방 메시지는 삭제 불가** (Slack 정책). 본인만.
- **Compliance Hold / Legal Hold** 적용된 워크스페이스는 `chat.delete` 거부됨 (관리자 정책)
- **편집 이력 (edited history)** 은 Slack 측에 남을 수 있음 (vendor 보존 기간)
- **Vendor 백업본** 은 vendor SLA 따라 정리 (보통 30~90일)
