# -*- coding: utf-8 -*-
"""
Discord bot: يبحث عن شركات (برمجة/ماركتنج/إيكومرس/هيلب ديسك) في مصر
ويرسل النتائج كـ Embeds + CSV، مع قوائم منسدلة.

* تم استبدال Serper.dev بـ DuckDuckGo (DDGS sync داخل thread).
* قراءة القيم الحساسة من Environment Variables (بدون أسرار في الكود).
"""

import os
import re, csv, io, asyncio
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()  # يقرأ من ملف .env محليًا (لو موجود)

import aiohttp
from bs4 import BeautifulSoup
import tldextract
import phonenumbers

import discord
from discord import app_commands
from discord.ext import commands
from duckduckgo_search import DDGS  # النسخة السنكرونس المتوافقة

# =========================
# الإعدادات (من المتغيرات البيئية)
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID_ENV = os.getenv("GUILD_ID", "").strip()
GUILD_ID = int(GUILD_ID_ENV) if GUILD_ID_ENV.isdigit() else None

DEFAULT_CITY = os.getenv("DEFAULT_CITY", "الغربية").strip()
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
DEFAULT_PER_GOV_WHEN_ALL = int(os.getenv("DEFAULT_PER_GOV_WHEN_ALL", "5"))

# =========================
# التصنيفات + عبارات البحث
# =========================
CATEGORIES = {
    "programming": {
        "ar": "شركات برمجة",
        "queries": ["شركة برمجة", "شركة برمجيات", "software company", "web development"],
    },
    "marketing": {
        "ar": "شركات ماركتينج",
        "queries": ["شركة تسويق إلكتروني", "شركة ماركتينج", "digital marketing agency", "إدارة سوشيال ميديا"],
    },
    "ecommerce": {
        "ar": "شركات ايكومرس",
        "queries": ["شركة إي-كومرس", "بناء متجر إلكتروني", "ecommerce agency", "woocommerce shop"],
    },
    "helpdesk": {
        "ar": "Help Desk / IT Support",
        "queries": ["دعم فني شركات", "help desk company", "IT support", "صيانة شبكات"],
    },
}

# =========================
# محافظات مصر
# =========================
EGYPT_GOVS = [
    "كل المحافظات",
    "القاهرة", "الجيزة", "الإسكندرية", "الدقهلية", "البحر الأحمر", "البحيرة", "الفيوم",
    "الغربية", "الإسماعيلية", "المنوفية", "المنيا", "القليوبية", "الوادي الجديد", "السويس",
    "أسوان", "أسيوط", "بني سويف", "بورسعيد", "دمياط", "الشرقية", "جنوب سيناء", "كفر الشيخ",
    "مطروح", "الأقصر", "قنا", "شمال سيناء", "سوهاج",
]

GHARBIA_TOWNS = ["طنطا", "المحلة الكبرى", "كفر الزيات", "زفتى", "سمنود", "السنطة", "بسيون", "قطور"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DiscordDataBot/1.0; +https://discord.com)",
    "Accept-Language": "ar,en;q=0.9",
}

# =========================
# Discord bot bootstrap
# =========================
intents = discord.Intents.default()
# لو هتحتاج قراءة محتوى الرسائل ارفع الـ intent اللي تحت وفعل Message Content من البورتال:
# intents.message_content = True

BOT = commands.Bot(command_prefix="!", intents=intents)

# =========================
# نماذج/مساعدات
# =========================
@dataclass
class Company:
    name: str
    website: Optional[str]
    phone: Optional[str]
    email: Optional[str]
    socials: List[str]
    city: Optional[str]
    category: str
    snippet: Optional[str]

def trim(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else (text[: max(0, limit - 3)] + "...")

def normalize_phone(text: str) -> Optional[str]:
    for match in phonenumbers.PhoneNumberMatcher(text, "EG"):
        num = phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
        return num
    return None

def extract_emails(text: str) -> List[str]:
    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    return list(dict.fromkeys(emails))[:3]

def looks_like_social(url: str) -> bool:
    return any(s in url for s in ["facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com", "linkedin.com"])

def guess_city_from_text(text: str) -> Optional[str]:
    for t in GHARBIA_TOWNS:
        if t in text:
            return t
    for gov in EGYPT_GOVS:
        if gov != "كل المحافظات" and gov in text:
            return gov
    return None

def clean_title(title: str) -> str:
    return re.sub(r"\s*\|\s*.*$", "", title).strip()[:80]

def domain_of(url: str) -> str:
    ext = tldextract.extract(url or "")
    if not ext.domain:
        return ""
    return f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain

# =========================
# DuckDuckGo Search
# =========================
async def ddg_search(query: str, place: str, max_results: int = 10) -> List[Dict[str, Any]]:
    keywords = f"{query} {place} مصر"

    def _run_sync():
        rows = []
        with DDGS() as ddgs:
            for r in ddgs.text(
                keywords=keywords,
                region="xa-ar",
                safesearch="moderate",
                timelimit=None,
                max_results=max_results,
            ):
                rows.append(r)
        return rows

    raw = await asyncio.to_thread(_run_sync)

    results: List[Dict[str, Any]] = []
    for r in raw:
        link = (r.get("href") or r.get("link") or "").strip()
        title = (r.get("title") or "").strip()
        body  = (r.get("body")  or r.get("snippet") or "").strip()
        if link.startswith("http"):
            results.append({"title": title, "link": link, "snippet": body})
    return results

# =========================
# جلب صفحة وتحليلها
# =========================
async def fetch_html(session: aiohttp.ClientSession, url: str) -> Tuple[str, str]:
    try:
        async with session.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT, allow_redirects=True, ssl=False) as r:
            if r.status != 200:
                return "", ""
            text = await r.text(errors="ignore")
            return text, str(r.url)
    except Exception:
        return "", ""

def parse_company_from_html(html: str, resolved_url: str, base_name: Optional[str], category_key: str, snippet: Optional[str], place_label: str) -> Company:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else (base_name or "Unknown")
    title = clean_title(title)

    visible_text_parts = []
    for tag in soup.find_all(string=True):
        if getattr(tag, "parent", None) and tag.parent.name in ["script", "style", "noscript"]:
            continue
        text = str(tag).strip()
        if text:
            visible_text_parts.append(text)
    full_text = "\n".join(visible_text_parts)[:20000]

    phone = normalize_phone(full_text) or normalize_phone(str(soup))
    emails = extract_emails(full_text)
    socials = [a.get("href") for a in soup.find_all("a", href=True) if looks_like_social(a["href"])]
    socials = list(dict.fromkeys(socials))[:5]

    city = guess_city_from_text(full_text) or guess_city_from_text(snippet or "") or place_label

    return Company(
        name=title,
        website=resolved_url,
        phone=phone,
        email=emails[0] if emails else None,
        socials=socials,
        city=city,
        category=CATEGORIES[category_key]["ar"],
        snippet=snippet[:180] if snippet else None
    )

# =========================
# البروسس الأساسية
# =========================
async def gather_companies_for_place(category_key: str, place_label: str, limit: int) -> List[Company]:
    results: List[Company] = []
    seen_domains = set()

    async with aiohttp.ClientSession() as session:
        all_hits = []
        for q in CATEGORIES[category_key]["queries"]:
            hits = await ddg_search(q, place_label, max_results=10)
            all_hits.extend(hits)
            await asyncio.sleep(0.25)

        cleaned = []
        for h in all_hits:
            link = h.get("link")
            if not link or not link.startswith("http"):
                continue
            d = domain_of(link)
            if not d or d in seen_domains:
                continue
            seen_domains.add(d)
            cleaned.append(h)

        cleaned = cleaned[: limit * 3]

        for hit in cleaned:
            html, final_url = await fetch_html(session, hit["link"])
            if not html:
                continue
            comp = parse_company_from_html(html, final_url, hit.get("title"), category_key, hit.get("snippet"), place_label)
            if not (comp.phone or comp.email or comp.socials):
                continue
            results.append(comp)
            if len(results) >= limit:
                break
            await asyncio.sleep(0.1)

    return results

def to_csv_bytes(items: List[Company]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "City", "Category", "Website", "Phone", "Email", "Socials", "Snippet"])
    for c in items:
        writer.writerow([
            c.name, c.city, c.category, c.website or "", c.phone or "", c.email or "",
            " | ".join(c.socials) if c.socials else "", c.snippet or ""
        ])
    return buf.getvalue().encode("utf-8-sig")

def company_to_embed(c: Company) -> discord.Embed:
    em = discord.Embed(
        title=trim(c.name, 250),
        url=c.website if c.website else discord.Embed.Empty,
        description=trim(c.snippet or "", 900),
        color=0x2ecc71,
        timestamp=datetime.utcnow()
    )
    em.add_field(name="📍 المحافظة/المدينة", value=trim(c.city or "—", 256), inline=True)
    em.add_field(name="🏷️ التصنيف", value=trim(c.category, 256), inline=True)
    if c.phone:
        em.add_field(name="☎️ التليفون", value=trim(f"`{c.phone}`", 1024), inline=False)
    if c.email:
        em.add_field(name="✉️ الإيميل", value=trim(f"`{c.email}`", 1024), inline=False)
    if c.socials:
        socials_md = "\n".join(f"[رابط]({s})" for s in c.socials[:5])
        em.add_field(name="🔗 سوشيال", value=trim(socials_md, 1024), inline=False)
    if c.website:
        em.set_footer(text=domain_of(c.website))
    return em

# =========================
# أوتوكومبليت للمحافظة
# =========================
async def city_autocomplete(_: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    current = (current or "").strip()
    options = []
    for name in EGYPT_GOVS:
        if not current or current in name:
            options.append(app_commands.Choice(name=name, value=name))
        if len(options) >= 25:
            break
    return options

# =========================
# Slash Commands
# =========================
@BOT.event
async def on_ready():
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            await BOT.tree.sync(guild=guild)
        else:
            await BOT.tree.sync()
        print(f"✅ Logged in as {BOT.user} (synced commands)")
    except Exception as e:
        print("Sync error:", e)

@BOT.tree.command(name="scan", description="ابحث عن شركات في محافظة محددة وأرسل النتائج كـ Embeds + CSV")
@app_commands.describe(category="التصنيف", city="المحافظة (اختياري؛ الافتراضي: الغربية)", limit="عدد النتائج (اختياري)")
@app_commands.choices(
    category=[
        app_commands.Choice(name="برمجة", value="programming"),
        app_commands.Choice(name="ماركتينج", value="marketing"),
        app_commands.Choice(name="إيكومرس", value="ecommerce"),
        app_commands.Choice(name="Help Desk / IT", value="helpdesk"),
    ],
    limit=[
        app_commands.Choice(name="5", value=5),
        app_commands.Choice(name="10", value=10),
        app_commands.Choice(name="15", value=15),
        app_commands.Choice(name="20", value=20),
        app_commands.Choice(name="25", value=25),
    ]
)
@app_commands.autocomplete(city=city_autocomplete)
async def scan(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    city: Optional[str] = None,
    limit: Optional[app_commands.Choice[int]] = None
):
    await interaction.response.defer(thinking=True)

    category_key = category.value
    place_label = (city or DEFAULT_CITY).strip()
    max_results = (limit.value if limit else 10)

    if place_label == "كل المحافظات":
        place_list = [g for g in EGYPT_GOVS if g != "كل المحافظات"]
        per_gov = min(max_results, DEFAULT_PER_GOV_WHEN_ALL)
        await interaction.followup.send(
            f"🔎 ببحث عن **{CATEGORIES[category_key]['ar']}** في **كل المحافظات** (≈{per_gov} نتيجة/محافظة)…"
        )
        all_items: List[Company] = []
        for gov in place_list:
            comps = await gather_companies_for_place(category_key, gov, per_gov)
            all_items.extend(comps)
            for c in comps:
                await interaction.followup.send(embed=company_to_embed(c))
                await asyncio.sleep(0.1)

        csv_bytes = to_csv_bytes(all_items)
        file = discord.File(io.BytesIO(csv_bytes), filename=f"{category_key}_all_egypt.csv")
        msg = "⚠️ لا توجد نتائج مناسبة حالياً." if not all_items else "📎 ملف CSV بكل النتائج:"
        return await interaction.followup.send(msg, file=file)

    companies = await gather_companies_for_place(category_key, place_label, max_results)
    if not companies:
        return await interaction.followup.send("⚠️ لم أعثر على نتائج كافية. جرّب تغيير المحافظة أو قلّل العدد.")

    for c in companies:
        await interaction.followup.send(embed=company_to_embed(c))
        await asyncio.sleep(0.1)

    csv_bytes = to_csv_bytes(companies)
    file = discord.File(io.BytesIO(csv_bytes), filename=f"{category_key}_{place_label}.csv")
    await interaction.followup.send("📎 ملف CSV بكل النتائج:", file=file)

@BOT.tree.command(name="scan_all", description="ابحث في كل التصنيفات لمحافظة واحدة (Embeds + CSV)")
@app_commands.describe(city="المحافظة (اختياري؛ الافتراضي: الغربية)", per_category="عدد النتائج لكل تصنيف (اختياري)")
@app_commands.choices(
    per_category=[
        app_commands.Choice(name="3", value=3),
        app_commands.Choice(name="5", value=5),
        app_commands.Choice(name="8", value=8),
        app_commands.Choice(name="10", value=10),
        app_commands.Choice(name="12", value=12),
    ]
)
@app_commands.autocomplete(city=city_autocomplete)
async def scan_all(
    interaction: discord.Interaction,
    city: Optional[str] = None,
    per_category: Optional[app_commands.Choice[int]] = None
):
    await interaction.response.defer(thinking=True)

    place_label = (city or DEFAULT_CITY).strip()
    per_cat = (per_category.value if per_category else 5)

    if place_label == "كل المحافظات":
        await interaction.followup.send(
            "ℹ️ لاستخدام جميع المحافظات مع كل التصنيفات استخدم الأمر **/scan** واختر التصنيف ثم «كل المحافظات»."
        )
        return

    all_companies: List[Company] = []
    for cat_key in CATEGORIES.keys():
        await interaction.followup.send(f"🔎 ببحث عن: **{CATEGORIES[cat_key]['ar']}** في **{place_label}** …")
        comps = await gather_companies_for_place(cat_key, place_label, per_cat)
        all_companies.extend(comps)
        for c in comps:
            await interaction.followup.send(embed=company_to_embed(c))
            await asyncio.sleep(0.1)

    if not all_companies:
        return await interaction.followup.send("⚠️ لا توجد نتائج مناسبة حالياً.")

    csv_bytes = to_csv_bytes(all_companies)
    file = discord.File(io.BytesIO(csv_bytes), filename=f"all_categories_{place_label}.csv")
    await interaction.followup.send("📎 ملف موحد بكل النتائج:", file=file)

# =========================
# تشغيل البوت
# =========================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("❌ فضلاً ضع قيمة صحيحة لـ DISCORD_TOKEN في Environment Variables.")
    BOT.run(DISCORD_TOKEN)
