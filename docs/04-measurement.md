# 측정

## 지표 정의

| 지표 | 정의 | GA4 원천 |
|---|---|---|
| 캠페인 외부 착지 세션 | `utm_campaign=gtm_7d_20261024` 세션 (테스트 `gtm_qa` 제외) | `sessionCampaignName` |
| 네이버 외 외부 유입 | 세션 시작 리퍼러가 네이버·자기 사이트·없음이 아닌 세션 | `pageReferrer` (session_start) |
| 재방문율 | 재방문 사용자 ÷ 활성 사용자 | `return_visit` 이벤트 사용자 |
| 전환율 | 공유·관심 저장·비교 도달 중 하나라도 한 사용자 ÷ 활성 사용자 | `share`, `save_item`, `compare_view` |
| 평균 세션 시간 | 세션 가중 평균 | `averageSessionDuration` |
| 세션당 페이지뷰 | | `screenPageViewsPerSession` |
| 참여율 | | `engagementRate` |
| 게시 1건당 착지 | 캠페인 세션 ÷ 게시된 답변 수 (placement별) | `sessionManualTerm` |

## UTM 규칙

바깥에 게시하는 링크에만 붙인다. 내부 링크에는 붙이지 않는다.
`/share/compare`의 canonical은 홈이라 UTM이 붙은 주소가 색인 중복을 만들지 않는다(2026-10-24 확인).

| 파라미터 | 값 |
|---|---|
| `utm_campaign` | `gtm_7d_20261024` (테스트는 `gtm_qa`) |
| `utm_source` | `naver_cafe` · `naver_kin` · `naver_blog` · `daum_cafe` · `daum_web` · `ppomppu` · `copy` · `web_share` |
| `utm_medium` | 커뮤니티 게시는 `community`, 복사·브라우저 공유는 `share` |
| `utm_content` | `compare` · `complex` · `hub` · `housing` · `screener` |
| `utm_term` | 게시 위치 코드 `placement_id` (예: `naver_cafe-1024-1`). 게시물 원문·닉네임은 넣지 않는다 |

## 증분 판정

네이버 검색 유입은 해커톤과 무관하게 크고 있다(7일 +61%). 그래서 성과를 이렇게 나눈다.

1. **직접 증분**: 캠페인 세션과 그 세션의 비교 도달·재방문. 우리가 만든 경로라 전부 증분이다.
2. **네이버 외 증분**: 리퍼러 기준 네이버 외 외부 유입(기준 주 66명)의 증가분. 카페 리퍼러는 캠페인과 겹칠 수 있어 합산하지 않고 따로 본다.
3. **네이버 검색**: 성과로 세지 않고 템플릿별 추이만 본다.
4. **질 지표**: 전체 평균은 새 네이버 유입에 희석되므로 캠페인 착지만 따로 본다.

## 판독 일정

- 매일 아침 [results/daily-log.md](../results/daily-log.md)에 전날 숫자를 적는다.
- GA는 최근 1~2일 값을 늦게 채운다(10/23 세션 시간 60.6초 → 재조회 85.1초). 최근 1~2일로 추세를 말하지 않고, 최종 판독은 D9에 한다.
- 실행: `python analysis/ga_report.py --start 2026-10-24 --end 2026-10-30`
