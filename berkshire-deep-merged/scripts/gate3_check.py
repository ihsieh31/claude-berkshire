#!/usr/bin/env python3
"""閘門 3（能力圈與護城河固定問卷）驗證。

  python gate3_check.py --answers answers.json [--questionnaire ../assets/gate3_questionnaire.json] [--asof YYYY-MM-DD]

answers.json 格式：
  {"ticker": "...", "decision_date": "YYYY-MM-DD", "answered_by": "agent 或人名", "answered_at": "YYYY-MM-DD HH:MM 時區",
   "price_seen_before_answering": false, "price_file_opened_at": "YYYY-MM-DD HH:MM 時區（作答完成後才填）",
   "answers": {"C1": {"answer": "10年寫得出", "reason": "...", "evidence": [{"source": "年報第幾頁／申報文件", "date": "YYYY-MM-DD"}]}, ...}}
輸出：{"gate3_pass": bool, "failures": [...], "strong_count": n, "max_grade_from_C1": "A|B|None",
      "timing_checked": bool（answered_at 與 price_file_opened_at 都有填、已比對先後才為 true）}。
任何一題缺答、作答格式錯（該題不是物件、evidence 不是物件清單）、缺理由、缺證據、證據日期不是嚴格 YYYY-MM-DD 或不早於決策日、
答案不在通過選項、護城河三測「強」少於 2、或作答前看過價格 → 不通過。
證據日期（evidence.date）＝該文件的 SEC 申報日 filingDate（非 SEC 文件用發布日），不是報告期末日；只能引用資料包已收錄的文件
（決策日前已申報者）。本腳本只能比對日期早於決策日：以期末日（如 2025-12-31）引用決策日後才申報的 10-K 也會通過檢查，
所以作答必須照此填（A3-M2D-01）。
「不知道」：C2–C4、M1–M4 有此選項（一律不通過＝太難）；C1、C5、M5、M6 沒有，資料不足時選最保守的選項並在 reason 註明（A3-M2A-07）。給 asof（框架的決策日）時，決策日以 asof 為準，作答檔 decision_date 與它不同即不通過
（不採信作答檔自填的決策日，M2_D6_05）。
價格時序（M2-D10-04）：正式流程由框架保證——FULL_B 開價格檔前以 gate3_answers.json 的檔案時間早於開檔時間為閘，
開檔時間寫入 price_file_opened_at.txt（路徑記在決策日誌 price_file_opened_at_file）；作答檔的 price_file_opened_at
在 FULL_A 時必為空（null、空字串或省略皆可），所以下面的先後比對只在兩欄都有填時（例如人工作答）才做，timing_checked 表示是否做過。
兩欄以時間解析後比對（不是字串比較；A3-M2A-08）：ISO 8601（「YYYY-MM-DD HH:MM」或 T 分隔，可帶秒）＋可選時區
（+08:00、Z、UTC 或 IANA 名稱如 Asia/Taipei，可空一格）；沒寫時區視為 Asia/Taipei。解析不了 → 不通過（無法確認價格隔離）。
等級（D18）：本系統是宿主模式＝小資金，M6＝良好不降級（良好型重資產已在 G3 排除）；「良好降一級」只適用非小資金模式，
所以本腳本只輸出 C1 上限。
不通過只代表「不得自動買」；結論最多觀察，理由寫入決策日誌。"""
import argparse, datetime, json, os, re
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
QPATH = os.path.join(HERE, '..', 'assets', 'gate3_questionnaire.json')


def _date(v):
    """嚴格 YYYY-MM-DD（去頭尾空白）→ date；其他寫法（10/20/2026、2026、20261020）→ None。"""
    s = str(v if v is not None else '').strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', s):
        return None
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


DEFAULT_TZ = ZoneInfo('Asia/Taipei')   # 作答時間沒寫時區時的假設（使用者所在時區）


def _when(v):
    """answered_at／price_file_opened_at → 帶時區的 datetime；解析不了 → None。"""
    s = str(v or '').strip()
    m = re.fullmatch(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*(.*)', s)
    if not m:
        return None
    base, tz = m.group(1), m.group(2).strip()
    try:
        if not tz:
            return datetime.datetime.fromisoformat(base).replace(tzinfo=DEFAULT_TZ)
        if tz in ('Z', 'UTC', 'GMT'):
            return datetime.datetime.fromisoformat(base).replace(tzinfo=datetime.timezone.utc)
        if re.fullmatch(r'[+-]\d{2}:?\d{2}', tz):
            return datetime.datetime.fromisoformat(base + tz[:3] + ':' + tz[-2:])
        return datetime.datetime.fromisoformat(base).replace(tzinfo=ZoneInfo(tz))
    except (ValueError, KeyError, OSError):
        return None


def _obj(v):
    """作答的一題：dict 照用；其他型別（字串、清單）→ {}（作答格式錯另記）。"""
    return v if isinstance(v, dict) else {}


def evaluate(ans, q=None, asof=None):
    q = q or json.load(open(QPATH, encoding='utf-8'))
    fails = []; A = ans.get('answers', {})
    if not isinstance(A, dict):
        fails.append('answers 不是 {題號: 作答} 物件（作答格式錯）'); A = {}
    own = _date(ans.get('decision_date'))
    D = _date(str(asof)[:10]) if asof else own
    if ans.get('price_seen_before_answering') is not False:
        fails.append('作答前看過價格或未聲明（價格最後看，規則書 CORE-03）')
    if D is None:
        fails.append('缺決策日或決策日不是 YYYY-MM-DD')
    elif asof and own != D:
        fails.append(f"作答檔決策日 {ans.get('decision_date')!r} 與框架決策日 {D} 不同")
    # null、空字串、省略都算「留空」（str(None)＝'None' 不得當成有填；M2-R5-01）
    at, po = (str(ans.get(k) or '').strip() for k in ('answered_at', 'price_file_opened_at'))
    timing = False
    if po and at:
        t_at, t_po = _when(at), _when(po)
        if t_at is None or t_po is None:
            bad = '、'.join(n for n, t in (('answered_at', t_at), ('price_file_opened_at', t_po)) if t is None)
            fails.append(f'{bad} 時間格式錯（無法比對價格隔離先後）')
        else:
            timing = True
            if t_po <= t_at:
                fails.append('價格檔開啟時間不晚於作答完成時間（價格隔離）')
    for item in q['questions']:
        a = A.get(item['id'])
        if not a:
            fails.append(f"{item['id']} 未作答"); continue
        if not isinstance(a, dict):
            fails.append(f"{item['id']} 作答格式錯（須為 {{answer, reason, evidence}} 物件）"); continue
        if not str(a.get('reason', '')).strip():
            fails.append(f"{item['id']} 缺理由")
        ev = a.get('evidence') or []
        if not isinstance(ev, list) or not all(isinstance(e, dict) for e in ev):
            fails.append(f"{item['id']} 證據格式錯（須為 [{{source, date}}] 清單）")
        elif not ev or not all(str(e.get('source', '')).strip() and str(e.get('date', '')).strip() for e in ev):
            fails.append(f"{item['id']} 缺證據（來源＋日期）")
        elif any(_date(e['date']) is None for e in ev):
            fails.append(f"{item['id']} 證據日期不是 YYYY-MM-DD")
        elif D and any(_date(e['date']) >= D for e in ev):
            fails.append(f"{item['id']} 證據日期不早於決策日（前視）")
        if a.get('answer') not in item['pass_if']:
            fails.append(f"{item['id']} 答案「{a.get('answer')}」不在通過選項")
    rule = q['pass_rule']['min_strong_among']
    strong = sum(1 for i in rule['ids'] if _obj(A.get(i)).get('answer') == '強')
    if strong < rule['min']:
        fails.append(f'護城河核心三測只有 {strong} 項為強（需 ≥{rule["min"]}）')
    c1 = _obj(A.get('C1')).get('answer')
    max_grade = 'A' if c1 == '10年寫得出' else ('B' if c1 == '5到10年且10年方向寫得出' else None)
    unknown = sum(1 for i in q['questions'] if _obj(A.get(i['id'])).get('answer') == '不知道')
    return {'gate3_pass': not fails, 'failures': fails, 'strong_count': strong, 'unknown_count': unknown,
            'max_grade_from_C1': max_grade, 'timing_checked': timing,
            'questionnaire_version': q.get('version')}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--answers', required=True); ap.add_argument('--questionnaire', default=QPATH)
    ap.add_argument('--asof', help='決策日 YYYY-MM-DD（給定時作答檔 decision_date 必須相同）')
    a = ap.parse_args()
    r = evaluate(json.load(open(a.answers, encoding='utf-8')), json.load(open(a.questionnaire, encoding='utf-8')),
                 asof=a.asof)
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
