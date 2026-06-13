"""
实时行情预警监控 (GitHub Actions 专用)
独立运行，仅依赖 Python 标准库
"""
import json
import os
import smtplib
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.header import Header

CST = timezone(timedelta(hours=8))

PORTFOLIO = {
    "300015": {"name": "爱尔眼科", "shares": 14000, "cost": 12.0, "stop_loss_pct": 0.07, "take_profit_pct": 0.10, "support": 8.0, "resistance": 9.5},
    "002352": {"name": "顺丰控股", "shares": 3000, "cost": 49.0, "stop_loss_pct": 0.07, "take_profit_pct": 0.10, "support": 33.0, "resistance": 37.0},
    "600196": {"name": "复星医药", "shares": 2500, "cost": 26.0, "stop_loss_pct": 0.07, "take_profit_pct": 0.10, "support": 23.0, "resistance": 28.0},
    "300499": {"name": "高澜股份", "shares": 700, "cost": 38.0, "stop_loss_pct": 0.07, "take_profit_pct": 0.12, "support": 33.0, "resistance": 40.0},
    "601865": {"name": "福莱特", "shares": 1900, "cost": 13.0, "stop_loss_pct": 0.07, "take_profit_pct": 0.12, "support": 11.5, "resistance": 14.5},
    "002241": {"name": "歌尔股份", "shares": 500, "cost": 22.0, "stop_loss_pct": 0.07, "take_profit_pct": 0.10, "support": 20.0, "resistance": 24.0},
    "300576": {"name": "容大感光", "shares": 250, "cost": 5.0, "stop_loss_pct": 0.07, "take_profit_pct": 0.15, "support": 40.0, "resistance": 50.0},
    "002368": {"name": "太极股份", "shares": 300, "cost": 22.0, "stop_loss_pct": 0.07, "take_profit_pct": 0.10, "support": 20.0, "resistance": 25.0},
}

WATCH_LIST = {
    "000977": "浪潮信息", "603986": "兆易创新", "605358": "立昂微",
    "600667": "太极实业", "300433": "蓝思科技", "002938": "鹏鼎控股", "603019": "中科曙光",
}


def is_trading_time():
    now = datetime.now(CST)
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return 928 <= t <= 1502


def fetch_quotes(codes):
    sina_codes = []
    for c in codes:
        sina_codes.append(f"sh{c}" if c.startswith("6") else f"sz{c}")
    url = f"https://hq.sinajs.cn/list={','.join(sina_codes)}"
    req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("gbk")
    except Exception:
        return {}

    result = {}
    for line in text.strip().split("\n"):
        if not line.strip():
            continue
        try:
            parts = line.split('="')
            key = parts[0].split("hq_str_")[-1].strip()
            data = parts[1].strip('";').split(",")
            if len(data) < 32:
                continue
            result[key] = {
                "name": data[0], "price": float(data[3]),
                "prev_close": float(data[2]), "change_pct": round((float(data[3]) / float(data[2]) - 1) * 100, 2),
                "high": float(data[4]), "low": float(data[5]),
                "volume": int(data[8]) if data[8] else 0,
            }
        except (ValueError, IndexError):
            continue
    return result


def build_alerts(quotes):
    alerts = []
    for code, info in PORTFOLIO.items():
        q = quotes.get(code) or quotes.get(f"sz{code}") or quotes.get(f"sh{code}")
        if not q:
            continue
        price = q["price"]
        cost = info["cost"]
        name = info["name"]
        shares = info["shares"]
        pnl_pct = (price / cost - 1) * 100 if cost > 0 else 0

        stop_loss = cost * (1 - info["stop_loss_pct"])
        take_profit = cost * (1 + info["take_profit_pct"])

        if price <= stop_loss:
            alerts.append(("critical", f"止损预警！{name}({code}) 成本{cost:.2f} 止损{stop_loss:.2f} 现价{price:.2f} 亏损{pnl_pct:.1f}%"))
        if price >= take_profit:
            alerts.append(("info", f"止盈提示！{name}({code}) 成本{cost:.2f} 止盈{take_profit:.2f} 现价{price:.2f} 盈利{pnl_pct:.1f}%"))
        if info["support"] > 0 and price <= info["support"]:
            alerts.append(("warning", f"触及支撑！{name}({code}) 支撑{info['support']} 现价{price:.2f}"))
        if info["resistance"] > 0 and price >= info["resistance"]:
            alerts.append(("warning", f"突破压力！{name}({code}) 压力{info['resistance']} 现价{price:.2f}"))
        if abs(q["change_pct"]) >= 3:
            alerts.append(("warning", f"日内异动！{name}({code}) {q['change_pct']:+.1f}% 现价{price:.2f}"))

    return alerts


def send_alert_email(alerts, quotes):
    email_from = os.environ.get("EMAIL_SENDER", "")
    email_pass = os.environ.get("EMAIL_PASSWORD", "")
    email_to = os.environ.get("EMAIL_RECEIVERS", "adu0213@163.com")

    if not email_from or not email_pass:
        return False

    critical = [a for a in alerts if a[0] == "critical"]
    warnings = [a for a in alerts if a[0] == "warning"]
    infos = [a for a in alerts if a[0] == "info"]
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    html = f"""<html><body style="font-family:Microsoft YaHei;background:#f5f5f5;padding:20px">
<div style="max-width:800px;margin:auto;background:white;border-radius:10px;padding:20px">
<h2 style="color:#e74c3c">实时行情预警</h2>
<p style="color:#666">{now}</p>"""

    if critical:
        html += "<h3 style='color:#e74c3c'>紧急止损预警</h3>"
        for _, msg in critical:
            html += f'<div style="background:#ffeaea;border-left:4px solid #e74c3c;padding:10px;margin:8px 0">{msg}</div>'
    if warnings:
        html += "<h3 style='color:#e67e22'>重要预警</h3>"
        for _, msg in warnings:
            html += f'<div style="background:#fff8e1;border-left:4px solid #e67e22;padding:10px;margin:8px 0">{msg}</div>'
    if infos:
        html += "<h3 style='color:#27ae60'>提示</h3>"
        for _, msg in infos:
            html += f'<div style="background:#e8f5e9;border-left:4px solid #27ae60;padding:10px;margin:8px 0">{msg}</div>'

    html += "<h3>当前行情</h3><table style='width:100%;border-collapse:collapse'>"
    html += "<tr style='background:#333;color:white'><th>代码</th><th>名称</th><th>现价</th><th>涨跌</th></tr>"
    seen = set()
    for code, q in quotes.items():
        raw = code[2:] if len(code) > 2 and code[:2] in ("sh", "sz") else code
        if raw in seen:
            continue
        seen.add(raw)
        color = "#e74c3c" if q.get("change_pct", 0) < 0 else "#27ae60"
        html += f'<tr><td>{raw}</td><td>{q.get("name","")}</td><td>{q.get("price","")}</td><td style="color:{color}">{q.get("change_pct","")}%</td></tr>'
    html += "</table></div></body></html>"

    try:
        msg = MIMEText(html, "html", "utf-8")
        msg["From"] = email_from
        msg["To"] = email_to
        msg["Subject"] = Header(f"实时预警 {len(critical)}紧急 {len(warnings)}重要", "utf-8")
        with smtplib.SMTP("smtp.qq.com", 587) as s:
            s.starttls()
            s.login(email_from, email_pass)
            s.sendmail(email_from, email_to.split(","), msg.as_string())
        return True
    except Exception:
        return False


def main():
    now = datetime.now(CST)
    print(f"[{now.strftime('%H:%M:%S')}] 预警监控启动")

    if now.weekday() >= 5:
        print("周末休市，跳过")
        return
    if not is_trading_time():
        print("非交易时间，跳过")
        return

    all_codes = list(PORTFOLIO.keys()) + list(WATCH_LIST.keys())
    quotes = fetch_quotes(all_codes)

    print(f"行情获取: {len(quotes)} 条")

    for code, info in PORTFOLIO.items():
        q = quotes.get(code) or quotes.get(f"sz{code}") or quotes.get(f"sh{code}")
        if q:
            pnl = (q["price"] / info["cost"] - 1) * 100 if info["cost"] > 0 else 0
            d = "🔴" if pnl < 0 else "🟢"
            print(f"  {d} {info['name']:<6} 成本{info['cost']:<6.2f} 现价{q['price']:<8.2f} {pnl:+6.1f}%")

    alerts = build_alerts(quotes)
    if not alerts:
        print("✅ 无预警")
        return

    critical = len([a for a in alerts if a[0] == "critical"])
    warnings = len([a for a in alerts if a[0] == "warning"])
    print(f"预警: {critical}紧急 {warnings}重要 {len(alerts)-critical-warnings}提示")

    for level, msg in alerts:
        print(f"  [{level:>8}] {msg}")

    if critical > 0:
        ok = send_alert_email(alerts, quotes)
        print(f"{'✅ 邮件已发送' if ok else '❌ 邮件发送失败'}")
    else:
        print("无紧急预警，跳过邮件")


if __name__ == "__main__":
    main()
