# 측정

## 지표 정의

| 지표 | 정의 | GA4 원천 |
|---|---|---|
| 활성 사용자 | 기간 중 활동한 사람 수 (사람 필터) | `activeUsers` |
| WAU / MAU | 7일 / 28일 활성 | `active7DayUsers` / `active28DayUsers` |
| 재방문율 | 재방문 사용자 ÷ 활성 사용자 | `return_visit` 이벤트 사용자 수 |
| 전환율 | 공유·알림 구독·관심 저장·텔레그램 이동 중 하나라도 한 사용자 ÷ 활성 사용자 | `share`, `save_item`, 새 이벤트 `alert_subscribe`·`telegram_click` |
| 평균 세션 시간 | 세션 가중 평균 | `averageSessionDuration` |
| 세션당 페이지뷰 | | `screenPageViewsPerSession` |
| 참여율 | 10초 이상·전환·2페이지 이상 세션 비율 | `engagementRate` |
| 공유 1건당 유입 | 공유 UTM 세션 ÷ `share` 이벤트 | |

## UTM 규칙

모든 바깥 링크에 단다. 정식 주소(canonical)는 UTM 없는 주소로 고정돼 있어 색인에 영향이 없다.

| 경로 | utm_source | utm_medium | utm_campaign | utm_content |
|---|---|---|---|---|
| 우리 공유 버튼 | `reevl_share` | `share` | `gtm7` | `compare` / `complex` / `housing` |
| 카카오톡 공유 카드 | `kakao` | `share` | `gtm7` | `compare` / `complex` / `housing` |
| 카페에 붙이기 복사 | `cafe_copy` | `share` | `gtm7` | `compare` / `complex` |
| 텔레그램 채널 | `telegram` | `social` | `gtm7` | 글 종류 |
| 긱뉴스 | `geeknews` | `community` | `gtm7` | |
| 디스콰이엇 | `disquiet` | `community` | `gtm7` | |
| 개발 블로그 | `blog` | `referral` | `gtm7` | 플랫폼 이름 |

## 증분 판정

네이버 검색 유입은 해커톤과 무관하게 크고 있다(7일 +62%). 그래서 성과를 이렇게 나눈다.

1. **직접 증분**: `utm_campaign=gtm7` 세션과 그 사용자의 재방문·전환. 우리가 만든 경로라 전부 증분이다.
2. **네이버 외 증분**: 리퍼러 기준 네이버 외 외부 유입(기준 주 66명)의 증가분.
3. **네이버**: 성과로 세지 않고, 템플릿별 추이만 본다. 15% 넘게 빠지면 실험을 끈다.
4. **질 지표** (재방문·세션 시간·페이지뷰): 전체 평균과 함께 **해커톤이 건드린 착지만** 따로 본다. 전체 평균은 새로 들어온 네이버 사람에 희석된다.

## 판독 일정

- 매일 아침: [results/daily-log.md](../results/daily-log.md)에 전날 숫자를 적는다.
- GA는 최근 1~2일 출처가 비어 나온다. 채널은 리퍼러 기준으로 보고, 최종 판독은 D9에 한다.
