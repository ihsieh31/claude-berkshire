#!/usr/bin/env python3
"""閘門 3（能力圈與護城河固定問卷）驗證。

  python gate3_check.py --answers answers.json [--questionnaire ../assets/gate3_questionnaire.json]

answers.json 格式：
  {"ticker": "...", "decision_date": "YYYY-MM-DD", "answered_by": "agent 或人名", "answered_at": "YYYY-MM-DD HH:MM 時區",
   "price_seen_before_answering": false, "price_file_opened_at": "YYYY-MM-DD HH:MM 時區（作答完成後才填）",
   "answers": {"C1": {"answer": "10年寫得出", "reason": "...", "evidence": [{"source": "年報第幾頁／申報文件", "date": "YYYY-MM-DD"}]}, ...}}
輸出：{"gate3_pass": bool, "failures": [...], "strong_count": n, "max_grade_from_C1": "A|B|None"}。
任何一題缺答、缺理由、缺證據、證據日期不早於決策日、答案不在通過選項、護城河三測「強」少於 2、或作答前看過價格 → 不通過。
不通過只代表「不得自動買」；結論最多觀察，理由寫入決策日誌。"""
import argparse, json, os

HERE = os.path.dirname(os.path.abspath(__file__))
QPATH = os.path.join(HERE, '..', 'assets', 'gate3_questionnaire.json')


def evaluate(ans, q=None):
    q = q or json.load(open(QPATH, encoding='utf-8'))
    fails = []; A = ans.get('answers', {}); D = str(ans.get('decision_date', ''))
    if ans.get('price_seen_before_answering') is not False:
        fails.append('作答前看過價格或未聲明（價格最後看，規則書 CORE-03）')
    if not D:
        fails.append('缺決策日')
    at, po = str(ans.get('answered_at', '')), str(ans.get('price_file_opened_at', ''))
    if po and at and po <= at:
        fails.append('價格檔開啟時間不晚於作答完成時間（價格隔離）')
    for item in q['questions']:
        a = A.get(item['id'])
        if not a:
            fails.append(f"{item['id']} 未作答"); continue
        if not str(a.get('reason', '')).strip():
            fails.append(f"{item['id']} 缺理由")
        ev = a.get('evidence') or []
        if not ev or not all(str(e.get('source', '')).strip() and str(e.get('date', '')).strip() for e in ev):
            fails.append(f"{item['id']} 缺證據（來源＋日期）")
        elif D and any(str(e['date']) >= D for e in ev):
            fails.append(f"{item['id']} 證據日期不早於決策日（前視）")
        if a.get('answer') not in item['pass_if']:
            fails.append(f"{item['id']} 答案「{a.get('answer')}」不在通過選項")
    rule = q['pass_rule']['min_strong_among']
    strong = sum(1 for i in rule['ids'] if (A.get(i) or {}).get('answer') == '強')
    if strong < rule['min']:
        fails.append(f'護城河核心三測只有 {strong} 項為強（需 ≥{rule["min"]}）')
    c1 = (A.get('C1') or {}).get('answer')
    max_grade = 'A' if c1 == '10年寫得出' else ('B' if c1 == '5到10年且10年方向寫得出' else None)
    unknown = sum(1 for i in q['questions'] if (A.get(i['id']) or {}).get('answer') == '不知道')
    return {'gate3_pass': not fails, 'failures': fails, 'strong_count': strong, 'unknown_count': unknown,
            'max_grade_from_C1': max_grade,
            'questionnaire_version': q.get('version')}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--answers', required=True); ap.add_argument('--questionnaire', default=QPATH)
    a = ap.parse_args()
    r = evaluate(json.load(open(a.answers, encoding='utf-8')), json.load(open(a.questionnaire, encoding='utf-8')))
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
