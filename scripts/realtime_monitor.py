"""
实时行情预警监控 (GitHub Actions 专用)
独立运行，仅依赖 Python 标准库
聚焦主流股票，生成买卖提示与大盘行情报告
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

# ============================================================
# 持仓（已有仓位，仅做风控提醒，不下单）
# ============================================================
PORTFOLIO = {
    "300015": {"name": "爱尔眼科", "shares": 14000, "cost": 12.0, "support": 8.0, "resistance": 9.5},
    "002352": {"name": "顺丰控股", "shares": 3000, "cost": 49.0, "support": 33.0, "resistance": 37.0},
    "600196": {"name": "复星医药", "shares": 2500, "cost": 26.0, "support": 23.0, "resistance": 28.0},
    "300499": {"name": "高澜股份", "shares": 700, "cost": 38.0, "support": 33.0, "resistance": 40.0},
    "601865": {"name": "福莱特", "shares": 1900, "cost": 13.0, "support": 11.5, "resistance": 14.5},
    "002241": {"name": "歌尔股份", "shares": 500, "cost": 22.0, "support": 20.0, "resistance": 24.0},
    "300576": {"name": "容大感光", "shares": 250, "cost": 5.0, "support": 40.0, "resistance": 50.0},
    "002368": {"name": "太极股份", "shares": 300, "cost": 22.0, "support": 20.0, "resistance": 25.0},
}

# ============================================================
# 主流股票池（按行业分类，追踪大盘风向）
# ============================================================
MAINSTREAM = {
    "AI/科技": {
        "002230": ("科大讯飞", 42, 50), "002415": ("海康威视", 30, 36),
        "000063": ("中兴通讯", 32, 38), "603019": ("中科曙光", 55, 68),
        "000977": ("浪潮信息", 55, 62),
    },
    "半导体": {
        "688981": ("中芯国际", 55, 65), "002371": ("北方华创", 160, 190),
        "603501": ("韦尔股份", 90, 110), "600584": ("长电科技", 32, 38),
    },
    "新能源": {
        "300750": ("宁德时代", 200, 240), "002594": ("比亚迪", 260, 310),
        "601012": ("隆基绿能", 14, 18), "300274": ("阳光电源", 70, 85),
    },
    "消费": {
        "600519": ("贵州茅台", 1500, 1650), "000858": ("五粮液", 130, 150),
        "000333": ("美的集团", 65, 75), "600887": ("伊利股份", 28, 32),
    },
    "医药": {
        "600276": ("恒瑞医药", 45, 55), "600436": ("片仔癀", 210, 240),
        "603259": ("药明康德", 48, 58),
    },
    "金融": {
        "600036": ("招商银行", 38, 43), "300059": ("东方财富", 14, 18),
        "600030": ("中信证券", 20, 24), "601318": ("中国平安", 55, 63),
    },
    "汽车": {
        "601127": ("赛力斯", 100, 130), "000625": ("长安汽车", 14, 17),
        "300124": ("汇川技术", 58, 68),
    },
    "资源": {
        "601899": ("紫金矿业", 18, 21), "601600": ("中国铝业", 8, 10),
    },
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
            entry = {
                "name": data[0], "price": float(data[3]),
                "prev_close": float(data[2]), "change_pct": round((float(data[3]) / float(data[2]) - 1) * 100, 2),
                "high": float(data[4]), "low": float(data[5]),
                "volume": int(data[8]) if data[8] else 0,
            }
            result[key] = entry
            raw_code = key[2:] if len(key) > 2 else key
            result[raw_code] = entry
        except (ValueError, IndexError):
            continue
    return result


def get_recommendation(change_pct, price, support, resistance):
    if resistance > 0 and price >= resistance:
        return "突破↑", "关注突破有效性，放量可追"
    if price <= support:
        return "支撑↓", "触及支撑位，观察能否企稳"
    if change_pct >= 3:
        return "强势↑", f"日内涨{change_pct:.1f}%，领涨板块"
    if change_pct <= -3:
        return "弱势↓", f"日内跌{change_pct:.1f}%，暂避锋芒"
    if change_pct >= 1:
        return "偏强", "趋势向好，可逢低关注"
    if change_pct <= -1:
        return "偏弱", "趋势偏弱，观望为宜"
    return "中性", "窄幅震荡，等待方向"


def build_alerts(quotes):
    """持仓风控"""
    alerts = []
    for code, info in PORTFOLIO.items():
        q = quotes.get(code) or quotes.get(f"sz{code}") or quotes.get(f"sh{code}")
        if not q:
            continue
        price = q["price"]; cost = info["cost"]; name = info["name"]
        pnl = (price / cost - 1) * 100 if cost > 0 else 0

        if price <= cost * 0.93:
            alerts.append(("critical", f"⚠ {name}({code}) 亏损{pnl:.1f}% 现价{price:.2f}，成本{cost:.2f}"))
        elif price >= cost * 1.10:
            alerts.append(("info", f"✓ {name}({code}) 盈利{pnl:.1f}% 现价{price:.2f}"))
        if info["support"] > 0 and price <= info["support"] * 1.02:
            alerts.append(("warning", f"⇣ {name}({code}) 接近支撑{info['support']} 现价{price:.2f}"))
        if info["resistance"] > 0 and price >= info["resistance"] * 0.98:
            alerts.append(("warning", f"⇡ {name}({code}) 接近压力{info['resistance']} 现价{price:.2f}"))
    return alerts


def build_recommendations(quotes):
    """主流股票买卖建议"""
    all_recs = []
    sector_summary = {}

    for sector, stocks in MAINSTREAM.items():
        sector_data = []
        sector_change = 0
        sector_count = 0

        for code, (name, sup, res) in stocks.items():
            q = quotes.get(code) or quotes.get(f"sz{code}") or quotes.get(f"sh{code}")
            if not q:
                continue
            price = q["price"]; cp = q["change_pct"]
            rec, reason = get_recommendation(cp, price, sup, res)
            sector_data.append((cp, name, code, price, rec, reason))
            sector_change += cp; sector_count += 1

        if sector_data:
            avg = sector_change / sector_count
            sector_data.sort(key=lambda x: x[0], reverse=True)
            all_recs.append((sector, avg, sector_data))

    return all_recs


def send_email(all_recs, alerts, quotes, top_gainers, top_losers):
    email_from = os.environ.get("EMAIL_SENDER", "")
    email_pass = os.environ.get("EMAIL_PASSWORD", "")
    email_to = os.environ.get("EMAIL_RECEIVERS", "adu0213@163.com")
    if not email_from or not email_pass:
        return False

    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    crit_count = len([a for a in alerts if a[0] == "critical"])
    warn_count = len([a for a in alerts if a[0] == "warning"])

    html = f"""<html><body style="font-family:Microsoft YaHei;background:#f5f5f5;padding:20px">
<div style="max-width:800px;margin:auto;background:white;border-radius:10px;padding:20px">
<h2 style="color:#1a73e8">A股行情速报 & 买卖提示</h2>
<p style="color:#666">{now}</p>"""

    # 涨跌榜
    if top_gainers:
        html += "<h3>📈 涨幅前5</h3>"
        for n, c, p, cp in top_gainers[:5]:
            html += f'<div style="background:#e8f5e9;margin:4px 0;padding:6px">{n}({c}) {p:.2f} <b style="color:#27ae60">+{cp:.1f}%</b></div>'
    if top_losers:
        html += "<h3>📉 跌幅前5</h3>"
        for n, c, p, cp in top_losers[:5]:
            html += f'<div style="background:#ffeaea;margin:4px 0;padding:6px">{n}({c}) {p:.2f} <b style="color:#e74c3c">{cp:.1f}%</b></div>'

    # 持仓预警
    if alerts:
        html += "<h3>📋 持仓提醒</h3>"
        for lvl, msg in alerts:
            bg = "#ffeaea" if lvl == "critical" else "#fff8e1" if lvl == "warning" else "#e8f5e9"
            html += f'<div style="background:{bg};margin:4px 0;padding:6px">{msg}</div>'

    # 行业板块
    for sector, avg, stocks in all_recs:
        color = "#27ae60" if avg > 0 else "#e74c3c"
        html += f"<h3>{sector} <span style='color:{color};font-size:14px'>{avg:+.1f}%</span></h3>"
        for cp, name, code, price, rec, reason in stocks:
            rec_color = {"突破↑": "#e74c3c", "强势↑": "#27ae60", "偏强": "#27ae60",
                         "支撑↓": "#e67e22", "弱势↓": "#e74c3c", "偏弱": "#e67e22",
                         "中性": "#666"}.get(rec, "#666")
            cpc = "#27ae60" if cp >= 0 else "#e74c3c"
            html += f'<div style="margin:2px 0;font-size:13px">'
            html += f'<b>{name}</b> {price:.2f} <span style="color:{cpc}">{cp:+.1f}%</span> '
            html += f'<span style="color:{rec_color};font-weight:bold">[{rec}]</span> '
            html += f'<span style="color:#999">{reason}</span></div>'

    html += "</div></body></html>"

    try:
        msg = MIMEText(html, "html", "utf-8")
        msg["From"] = email_from; msg["To"] = email_to
        subject = f"行情速报 {crit_count}预警 {len(all_recs)}板块"
        msg["Subject"] = Header(subject, "utf-8")
        with smtplib.SMTP("smtp.qq.com", 587) as s:
            s.starttls(); s.login(email_from, email_pass)
            s.sendmail(email_from, email_to.split(","), msg.as_string())
        return True
    except Exception:
        return False


def main():
    now = datetime.now(CST)
    print(f"[{now.strftime('%H:%M:%S')}] 行情监控启动")

    if now.weekday() >= 5:
        print("周末休市，跳过"); return
    if not is_trading_time():
        print("非交易时间，跳过"); return

    # 收集所有股票代码
    all_codes = list(PORTFOLIO.keys())
    for stocks in MAINSTREAM.values():
        all_codes.extend(stocks.keys())
    quotes = fetch_quotes(all_codes)

    print(f"行情获取: {len(quotes)} 条")

    # 持仓盈亏
    print("\n===== 持仓 =====")
    for code, info in PORTFOLIO.items():
        q = quotes.get(code) or quotes.get(f"sz{code}") or quotes.get(f"sh{code}")
        if q:
            pnl = (q["price"] / info["cost"] - 1) * 100
            d = "🔴" if pnl < 0 else "🟢"
            print(f"  {d} {info['name']:<6} {q['price']:>8.2f} {pnl:+6.1f}% (成本{info['cost']})")

    # 板块行情
    print("\n===== 行业板块 =====")
    all_recs = build_recommendations(quotes)
    for sector, avg, stocks in all_recs:
        d = "🟢" if avg >= 0 else "🔴"
        print(f"\n  {d} {sector} ({avg:+.1f}%)")
        for cp, name, code, price, rec, reason in stocks[:3]:
            print(f"    {name:<6} {price:>8.2f} {cp:>+6.1f}% [{rec}] {reason}")

    # 涨跌榜
    all_stocks = []
    for stocks in MAINSTREAM.values():
        for code, (name, sup, res) in stocks.items():
            q = quotes.get(code) or quotes.get(f"sz{code}") or quotes.get(f"sh{code}")
            if q:
                all_stocks.append((q["change_pct"], name, code, q["price"]))
    all_stocks.sort(key=lambda x: x[0], reverse=True)
    top_gainers = [(n, c, p, cp) for cp, n, c, p in all_stocks[:5]]
    top_losers = [(n, c, p, cp) for cp, n, c, p in all_stocks[-5:]]

    print("\n===== 涨幅前5 =====")
    for n, c, p, cp in top_gainers:
        print(f"  🟢 {n}({c}) {p:.2f} +{cp:.1f}%")
    print("\n===== 跌幅前5 =====")
    for n, c, p, cp in top_losers:
        print(f"  🔴 {n}({c}) {p:.2f} {cp:.1f}%")

    # 持仓预警
    alerts = build_alerts(quotes)
    if alerts:
        print(f"\n===== 持仓预警 ({len(alerts)}) =====")
        for lvl, msg in alerts:
            print(f"  {lvl}: {msg}")

    # 发送邮件（有预警或尾盘才发）
    has_alerts = any(a[0] in ("critical", "warning") for a in alerts)
    is_close = now.hour >= 14 and now.minute >= 45
    if has_alerts or is_close:
        ok = send_email(all_recs, alerts, quotes, top_gainers, top_losers)
        print(f"\n{'✅ 邮件已发送' if ok else '❌ 邮件发送失败'}")
    else:
        print(f"\n行情正常，未发送邮件（尾盘自动发送总结）")


if __name__ == "__main__":
    main()
