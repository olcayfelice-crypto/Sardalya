import gzip
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

TUR_SAYISI = 4
ZAMAN_ASIMI = 15
UA = {"User-Agent": "Sardalya/1.0 (egitim amacli)",
      "Accept-Encoding": "gzip"}

DURAK = set(
    ("ve ile bir bu şu için gibi olan olarak daha çok en de da ki mi mı mu mü ne "
     "ama veya ya her tüm bazı sonra önce kadar üzerinde içinde arasında "
     "the and for are was were with that this from have has had not but you your "
     "which their they them been also can will its into such than then there these "
     "those who what when where how about more most other some over only both").split())

hatalar = []


def al(url):
    istek = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(istek, timeout=ZAMAN_ASIMI) as r:
        veri = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            veri = gzip.decompress(veri)
        return veri.decode("utf-8", errors="replace")


def al_json(url):
    return json.loads(al(url))


def q(metin):
    return urllib.parse.quote(metin)


def temizle(metin):
    metin = re.sub(r"<[^>]+>", " ", metin or "")
    return re.sub(r"\s+", " ", metin).strip()


def kelimeler_of(metin):
    return [k for k in re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü]{3,}", metin.lower())]


def anahtar_kelimeler(metin, haric="", n=3):
    yasak = set(kelimeler_of(haric)) | DURAK
    sayac = Counter(k for k in kelimeler_of(metin) if len(k) >= 5 and k not in yasak)
    return [k for k, _ in sayac.most_common(n)]


def bulgu(metin, kaynak, url):
    return {"metin": temizle(metin), "kaynak": kaynak, "url": url, "bot": ""}


def api_wikipedia(sorgu, dil="tr"):
    url = (f"https://{dil}.wikipedia.org/w/api.php?action=query&format=json"
           f"&generator=search&gsrsearch={q(sorgu)}&gsrlimit=3&prop=extracts"
           f"&exintro=1&explaintext=1&exchars=1200")
    sayfalar = al_json(url).get("query", {}).get("pages", {})
    sonuc = []
    for s in sorted(sayfalar.values(), key=lambda x: x.get("index", 99)):
        baslik = s.get("title", "")
        sonuc.append(bulgu(s.get("extract", ""), f"Wikipedia ({dil}): {baslik}",
                           f"https://{dil}.wikipedia.org/wiki/{q(baslik.replace(' ', '_'))}"))
    return sonuc


def api_duckduckgo(sorgu, dil="en"):
    d = al_json(f"https://api.duckduckgo.com/?q={q(sorgu)}&format=json"
                f"&no_html=1&skip_disambig=1")
    sonuc = []
    if d.get("AbstractText"):
        sonuc.append(bulgu(d["AbstractText"], "DuckDuckGo", d.get("AbstractURL", "")))
    for t in d.get("RelatedTopics", [])[:4]:
        if isinstance(t, dict) and t.get("Text"):
            sonuc.append(bulgu(t["Text"], "DuckDuckGo", t.get("FirstURL", "")))
    return sonuc


def api_arxiv(sorgu, dil="en"):
    xml = al(f"https://export.arxiv.org/api/query?search_query=all:{q(sorgu)}"
             f"&max_results=3&sortBy=relevance")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    sonuc = []
    for e in ET.fromstring(xml).findall("a:entry", ns):
        baslik = temizle(e.findtext("a:title", "", ns))
        ozet = temizle(e.findtext("a:summary", "", ns))
        sonuc.append(bulgu(ozet, f"arXiv: {baslik}", e.findtext("a:id", "", ns)))
    return sonuc


def api_crossref(sorgu, dil="en"):
    d = al_json(f"https://api.crossref.org/works?query={q(sorgu)}&rows=3"
                f"&select=title,abstract,URL,container-title")
    sonuc = []
    for it in d.get("message", {}).get("items", []):
        baslik = (it.get("title") or [""])[0]
        metin = it.get("abstract") or baslik
        sonuc.append(bulgu(metin, f"Crossref: {baslik}", it.get("URL", "")))
    return sonuc


def api_openlibrary(sorgu, dil="en"):
    d = al_json(f"https://openlibrary.org/search.json?q={q(sorgu)}&limit=3"
                f"&fields=title,author_name,first_publish_year,key")
    sonuc = []
    for k in d.get("docs", []):
        yazar = ", ".join(k.get("author_name", [])[:2]) or "bilinmeyen yazar"
        metin = (f"'{k.get('title')}' adlı kitap konuyla ilgili; yazar: {yazar}, "
                 f"ilk yayın yılı: {k.get('first_publish_year', '?')}.")
        sonuc.append(bulgu(metin, "Open Library", "https://openlibrary.org" + k.get("key", "")))
    return sonuc


def api_stackexchange(sorgu, dil="en"):
    d = al_json(f"https://api.stackexchange.com/2.3/search/advanced?order=desc"
                f"&sort=relevance&q={q(sorgu)}&site=stackoverflow&pagesize=3")
    return [bulgu(f"Toplulukta tartışılan soru: {i.get('title')}. "
                  f"({i.get('answer_count', 0)} cevap)", "StackOverflow", i.get("link", ""))
            for i in d.get("items", [])]


_hn = {}


def _hn_item(taban, i):
    try:
        return al_json(f"{taban}/item/{i}.json")
    except Exception:
        return None


def api_hackernews(sorgu, dil="en"):
    taban = "https://hacker-news.firebaseio.com/v0"
    if "items" not in _hn:
        idler = []
        for liste in ("topstories", "beststories", "newstories"):
            idler += al_json(f"{taban}/{liste}.json")[:50]
        idler = list(dict.fromkeys(idler))
        with ThreadPoolExecutor(10) as ex:
            _hn["items"] = [x for x in ex.map(lambda i: _hn_item(taban, i), idler) if x]
    kelimeler = [k for k in kelimeler_of(sorgu) if len(k) >= 4]
    sonuc = []
    for it in _hn["items"]:
        baslik = it.get("title", "")
        if kelimeler and any(k in baslik.lower() for k in kelimeler):
            sonuc.append(bulgu(f"Hacker News gündeminde: {baslik}. ({it.get('score', 0)} puan)",
                               "Hacker News (Firebase)",
                               it.get("url") or f"https://news.ycombinator.com/item?id={it['id']}"))
    return sonuc[:5]


BOTLAR = [
    {"ad": "Tanım Botu", "api": api_wikipedia, "dil": "tr"},
    {"ad": "Uluslararası Botu", "api": api_wikipedia, "dil": "en"},
    {"ad": "Hızlı Cevap Botu", "api": api_duckduckgo, "dil": "en"},
    {"ad": "Akademik Botu", "api": api_arxiv, "dil": "en"},
    {"ad": "Makale Botu", "api": api_crossref, "dil": "en"},
    {"ad": "Kitap Botu", "api": api_openlibrary, "dil": "en"},
    {"ad": "Topluluk Botu", "api": api_stackexchange, "dil": "en"},
    {"ad": "Trend Botu (Firebase)", "api": api_hackernews, "dil": "en"},
]


def guvenli(bot, sorgu):
    try:
        return bot["api"](sorgu, bot["dil"])
    except Exception as e:
        hatalar.append(f"{bot['ad']}: {type(e).__name__}")
        return []


def bot_calistir(bot, sorgu_tr, sorgu_en, ekstra):
    ana = sorgu_en if bot["dil"] == "en" else sorgu_tr
    ilk = (ana + " " + " ".join(ekstra)).strip()
    sonuc = guvenli(bot, ilk)
    yeni = anahtar_kelimeler(" ".join(b["metin"] for b in sonuc), haric=ana, n=2)
    if yeni:
        sonuc += guvenli(bot, ana + " " + " ".join(yeni))
    for b in sonuc:
        b["bot"] = bot["ad"]
    return sonuc


def ingilizce_karsilik(sorgu):
    try:
        url = (f"https://tr.wikipedia.org/w/api.php?action=query&format=json"
               f"&generator=search&gsrsearch={q(sorgu)}&gsrlimit=1&prop=langlinks&lllang=en")
        for s in al_json(url).get("query", {}).get("pages", {}).values():
            if s.get("langlinks"):
                return s["langlinks"][0]["*"]
    except Exception:
        pass
    return sorgu


def cevap_olustur(bulgular, sorgu, cumle_sayisi=8):
    sorgu_kelime = set(kelimeler_of(sorgu))
    cumleler = []
    for b in bulgular:
        for c in re.split(r"(?<=[.!?])\s+", b["metin"]):
            if 40 <= len(c) <= 400:
                cumleler.append((c, b))
    frekans = Counter(k for c, _ in cumleler for k in kelimeler_of(c)
                      if len(k) >= 4 and k not in DURAK)
    puanli = []
    for c, b in cumleler:
        kel = [k for k in set(kelimeler_of(c)) if len(k) >= 4 and k not in DURAK]
        if not kel:
            continue
        puan = sum(frekans[k] for k in kel) / (len(kel) ** 0.5)
        puan += 5 * len(sorgu_kelime & set(kelimeler_of(c)))
        puanli.append((puan, c, b))
    secilen, gorulen = [], set()
    for puan, c, b in sorted(puanli, key=lambda x: -x[0]):
        anahtar = c[:60].lower()
        if anahtar in gorulen:
            continue
        gorulen.add(anahtar)
        secilen.append((c, b))
        if len(secilen) >= cumle_sayisi:
            break
    return secilen


def cevap_yaz(secilen):
    return "\n".join(f"• {c}  [{b['bot']}]" for c, b in secilen)


def arastir(sorgu):
    print(f"\n🔎 Araştırılıyor: {sorgu}")
    sorgu_en = ingilizce_karsilik(sorgu)
    if sorgu_en != sorgu:
        print(f"   (İngilizce karşılık: {sorgu_en})")

    tum, gorulen, ekstra, kullanilan = [], set(), [], set()
    rapor = [f"Araştırma: {sorgu}", f"{datetime.now():%d.%m.%Y %H:%M}\n"]
    secilen = []

    for tur in range(1, TUR_SAYISI + 1):
        print(f"\n━━ TUR {tur}/{TUR_SAYISI} "
              f"{'(ilk araştırma)' if tur == 1 else '(derinleştirme: ' + ', '.join(ekstra) + ')'}")
        with ThreadPoolExecutor(len(BOTLAR)) as ex:
            sonuclar = list(ex.map(lambda b: bot_calistir(b, sorgu, sorgu_en, ekstra), BOTLAR))
        yeni_sayi = 0
        for bot, liste in zip(BOTLAR, sonuclar):
            for b in liste:
                anahtar = (b["url"], b["metin"][:80])
                if b["metin"] and anahtar not in gorulen:
                    gorulen.add(anahtar)
                    tum.append(b)
                    yeni_sayi += 1
            print(f"   {bot['ad']:<24} {len(liste)} bulgu")
        print(f"   → bu turda {yeni_sayi} yeni bilgi, toplam {len(tum)}")

        secilen = cevap_olustur(tum, sorgu + " " + sorgu_en)
        metin = cevap_yaz(secilen)
        print(f"\n📝 TUR {tur} CEVABI:\n{metin}")
        rapor += [f"Tur {tur} cevabı", metin, ""]

        kullanilan |= set(kelimeler_of(sorgu + " " + sorgu_en)) | set(ekstra)
        ekstra = anahtar_kelimeler(" ".join(c for c, _ in secilen),
                                   haric=" ".join(kullanilan), n=2)

    kaynaklar = list(dict.fromkeys(f"- {b['kaynak']} — {b['url']}" for _, b in secilen if b["url"]))
    kaynaklar += [k for k in dict.fromkeys(f"- {b['kaynak']} — {b['url']}" for b in tum if b["url"])
                  if k not in kaynaklar][:15]
    rapor += ["Kaynaklar"] + kaynaklar
    if hatalar:
        rapor += ["", "Çalışmayan kaynaklar: " + ", ".join(sorted(set(hatalar)))]

    os.makedirs("cikti", exist_ok=True)
    dosya = os.path.join("cikti", re.sub(r"\W+", "_", sorgu.lower())[:40] + ".txt")
    with open(dosya, "w", encoding="utf-8") as f:
        f.write("\n".join(rapor))
    print(f"\n✅ Bitti. Rapor kaydedildi: {dosya}")
    if hatalar:
        print("⚠️  Yanıt vermeyen kaynaklar:", ", ".join(sorted(set(hatalar))))
    hatalar.clear()


def main():
    if len(sys.argv) > 1:
        arastir(" ".join(sys.argv[1:]))
        return
    print("Sardalya — çıkmak için boş bırakıp Enter'a bas.")
    while True:
        try:
            sorgu = input("\nNeyi araştırayım? > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not sorgu:
            break
        arastir(sorgu)


if __name__ == "__main__":
    main()
