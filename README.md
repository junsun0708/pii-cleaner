# pii-cleaner

회사 사용 SaaS (Jira / Bitbucket / Slack / Microsoft 365) 의 개인정보를 식별·익명화·삭제하는 자동화 도구.

## 목적

퇴사자 또는 정리 대상의 개인 식별 정보 (PII) 가 4개 도구 전반에 흩어져있어
수동 정리는 누락·실수 위험. 도구별 API 어댑터로 일관된 정책을 적용한다.

## 핵심 원칙

- **dry-run 우선** — 실제 변경 전 「대상 목록 + 변경 미리보기」 산출
- **익명화 ≥ 삭제** — GDPR/개인정보보호법 권장. 회계·감사 추적 유지 가능
- **법정 보관기간 존중** — 계약·세무·인사 관련은 5~10년 보관 의무
- **컴플라이언스 승인 후 실행** — Litigation Hold 등 보존 정책 확인

## 도구별 삭제 대상 요약

| 도구 | 주요 대상 |
|---|---|
| Jira | 계정·이슈/댓글 PII·@mention·첨부파일·changelog·검색 인덱스 |
| Bitbucket | 커밋 author·메시지·코드 안 자격증명·PR·SSH 키·앱 토큰 |
| Slack | 프로필·공개 메시지·DM (Discovery)·파일·봇 메시지·앱 토큰 |
| MS365 | Entra ID·Exchange·Teams·OneDrive·SharePoint·Forms·Power Platform |

자세히는 `docs/00_삭제대상_도구별.md`.

## 문서

- [00_삭제대상_도구별.md](docs/00_삭제대상_도구별.md) — 4개 도구의 PII 위치 + API 매핑
- [01_법적고려사항.md](docs/01_법적고려사항.md) — 법정 보관기간·익명화·백업·eDiscovery
- [02_시나리오.md](docs/02_시나리오.md) — 퇴사자/외부고객/기간 시나리오별 우선순위
- [03_TODO.md](docs/03_TODO.md) — 진행 단계
