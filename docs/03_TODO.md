# 진행 단계

## Phase 0 — 문서 정리 ✅ (현재)
- [x] 도구별 PII 위치·API 매핑 (`00_삭제대상_도구별.md`)
- [x] 법적 고려사항 (`01_법적고려사항.md`)
- [x] 시나리오별 정책 (`02_시나리오.md`)
- [ ] 회사 실제 사용 도구 보강 — GitHub, Confluence, Notion, HR 시스템 등 (사용자 확인 후)

## Phase 1 — 어댑터 설계
- [ ] 공통 모델 — `Person`, `PIIRecord`, `Action`, `DryRunResult`
- [ ] 도구별 인터페이스 (`IAdapter`) — `find_pii`, `dry_run`, `execute`
- [ ] 4개 어댑터 시그니처 — Jira, Bitbucket, Slack, MS365

## Phase 2 — Jira 어댑터 (먼저)
- [ ] Atlassian API Token + Site Admin 권한 확보
- [ ] 사용자 검색·익명화 (GDPR Anonymize API)
- [ ] 이슈·댓글 grep + redact
- [ ] dry-run 결과 리포트

## Phase 3 — Bitbucket 어댑터
- [ ] 사용자 SSH 키·App password 삭제
- [ ] 레포 코드 grep (`git secrets` 패턴)
- [ ] PR·comment 삭제
- [ ] (옵션) `git filter-repo` 히스토리 재작성 — 별도 승인 워크플로우

## Phase 4 — Slack 어댑터
- [ ] User OAuth + Bot Token 발급
- [ ] 프로필 익명화
- [ ] 공개 메시지 chat.delete
- [ ] (Enterprise) Discovery API 로 DM 정리
- [ ] 첨부 파일 삭제

## Phase 5 — MS365 어댑터
- [ ] Azure App Registration (Microsoft Graph 권한)
- [ ] Entra ID 사용자 비활성/삭제
- [ ] Outlook 사서함 export + 정리
- [ ] Teams 채팅 Search-and-Purge
- [ ] OneDrive/SharePoint 콘텐츠 정리
- [ ] Litigation Hold 확인 로직

## Phase 6 — 통합 실행기
- [ ] CLI / Web UI (선택)
- [ ] 시나리오 A (퇴사자) 워크플로우 자동화
- [ ] 승인 단계 (dry-run → 검토 → 실행)
- [ ] 감사 로그 (Audit Trail)
- [ ] 결과 리포트 (HTML/PDF)

## Phase 7 — 운영
- [ ] 컴플라이언스 팀 사용 매뉴얼
- [ ] 정기 점검 (월 1회)
- [ ] 백업본 vendor 요청 절차 문서화

---

## 결정 필요 항목 (사용자 확인)

1. **회사 추가 사용 도구** — GitHub org? Confluence? Notion? HR (Greenhouse/사람인)? CRM (Salesforce)?
2. **개발 환경** — Python? Node? Java? (현재 PII grep 라이브러리 풍부한 Python 권장)
3. **CLI vs Web** — 컴플라이언스 팀이 직접 사용? 또는 개발자만?
4. **인증 보관** — Vault, AWS Secrets Manager, 환경변수, 1Password 등
5. **MVP 범위** — 4개 도구 동시 vs 시나리오 A (퇴사자) 만 우선
