# global-macro-data-collector

미국·한국 엔진과 분리된 글로벌 보조 수집기입니다.

## 역할
- ISM 공식 제조업 월간보고서 수집
- 신규주문, 재고, 생산, 고용, 공급업체 배송, 가격 저장
- 이전 정상 JSON 유지
- 매일 자동 실행 및 수동 실행

## 적용
1. GitHub에 `global-macro-data-collector` 저장소를 만듭니다.
2. 이 폴더의 파일을 저장소 루트에 업로드합니다.
3. Actions에서 `Update global macro data`를 수동 실행합니다.
4. Apps Script V59는 기본적으로 다음 주소를 읽습니다.
   `https://raw.githubusercontent.com/12xx37r-ui/global-macro-data-collector/main/public/data/ism_manufacturing.json`

다른 저장소명을 쓰면 Apps Script 속성에 아래를 추가합니다.
- 이름: `ISM_MANUFACTURING_JSON_URL`
- 값: 해당 raw JSON 주소


## 카드 8 — 미국채 금리·실질금리·수익률곡선
- 출력: `public/data/us_treasury_card8.json`
- 상태: `public/data/us_treasury_card8_status.json`
- 미국 정책금리 경로는 `fed-futures-collector`의 `latest.json`을 읽기 전용으로 사용합니다.
- 2년물·10년물·10년 실질금리·10년-2년 곡선을 5일·1개월·3개월·6개월·12개월로 분리합니다.
- expanding walk-forward OOS에서 지속성 기준모형보다 못한 후보는 자동 차단합니다.
- 기간·대상별 품질 게이트를 통과한 경우에만 준기관급으로 표시합니다.
