"""The PostHog project, as code: settings, actions, cohorts and every dashboard.

    POSTHOG_KEY=phx_... python posthog/setup.py [--check]

Run it again after adding a product to PRODUCTS: it rebuilds the dashboards it
owns (found by name) and leaves everything else alone. --check runs every query
against PostHog and stops at the first error, without changing anything.

The personal API key comes from the environment only; it never goes in a file.
Every event carries `product` (lumea, sotto, ...) and `channel` (app from the
beacon Worker, web from the sites), so each tile splits the same way.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

PROJECT = 323268
API = f"https://eu.posthog.com/api/projects/{PROJECT}"
UI = f"https://eu.posthog.com/project/{PROJECT}"

# Products with an app sending through beacon: one dashboard each. The sites
# use the same names for the product pages; `home` and `tools` are site-only.
PRODUCTS = {"lumea": "Lumea", "sotto": "sotto", "taplock": "TapLock"}
# Products whose released app sends through beacon: their "silent" alert is on.
# Until then nobody would be there to be silent, and the alert would fire daily.
LIVE = set()
# Tile names this script used before a rename: still its own, removed on the next run.
RETIRED = set()
SITES = {"apps": "https://ugurcandede.github.io/apps", "portfolio": "https://ugurcandede.github.io"}

# Recorded by beacon or PostHog themselves; "what people do" leaves them out.
SYSTEM_EVENTS = ["heartbeat", "daily_active", "app_open", "app_quit", "first_launch", "app_updated",
                 "update_started", "update_failed", "update_skipped", "$screen", "$set", "$identify", "$pageview",
                 "$pageleave", "$autocapture", "$web_vitals", "$rageclick", "$dead_click", "$exception",
                 "$groupidentify", "$opt_in", "$feature_flag_called"]

APP = {"key": "channel", "value": "app", "operator": "exact", "type": "event"}
WEB = {"key": "channel", "value": "web", "operator": "exact", "type": "event"}
LIVE_MINUTES = 10  # heartbeats come every 5; two missed means closed
ACTIONS = {}  # action name -> id, filled in by main() before the dashboards are built


# ---- query builders -------------------------------------------------------------


def event(name, math="total", props=(), **extra):
    node = {"kind": "EventsNode", "event": name, "math": math, "properties": list(props)}
    node.update(extra)
    return node


def trends(series, *, days=30, interval="day", display="ActionsLineGraph", breakdown=None,
           breakdown_type="event", formula=None, props=(), compare=False):
    source = {"kind": "TrendsQuery", "dateRange": {"date_from": f"-{days}d"}, "interval": interval,
              "series": series, "properties": list(props), "filterTestAccounts": True,
              "trendsFilter": {"display": display}}
    if formula:
        source["trendsFilter"]["formula"] = formula
    if breakdown:
        source["breakdownFilter"] = {"breakdown": breakdown, "breakdown_type": breakdown_type, "breakdown_limit": 25}
    if compare:
        source["compareFilter"] = {"compare": True}
    return {"kind": "InsightVizNode", "source": source}


def funnel(steps, *, days=30, window_days=14, breakdown=None):
    source = {"kind": "FunnelsQuery", "dateRange": {"date_from": f"-{days}d"}, "series": steps,
              "filterTestAccounts": True,
              "funnelsFilter": {"funnelWindowInterval": window_days, "funnelWindowIntervalUnit": "day",
                                "funnelVizType": "steps"}}
    if breakdown:
        source["breakdownFilter"] = {"breakdown": breakdown, "breakdown_type": "event"}
    return {"kind": "InsightVizNode", "source": source}


def retention(start, back, *, props=()):
    return {"kind": "InsightVizNode", "source": {
        "kind": "RetentionQuery", "dateRange": {"date_from": "-8w"}, "filterTestAccounts": True,
        "properties": list(props),
        "retentionFilter": {"period": "Week", "totalIntervals": 8, "retentionType": "retention_first_time",
                            "targetEntity": {"id": start, "type": "events"},
                            "returningEntity": {"id": back, "type": "events"}}}}


def lifecycle(props=()):
    return {"kind": "InsightVizNode", "source": {
        "kind": "LifecycleQuery", "dateRange": {"date_from": "-30d"}, "interval": "day",
        "filterTestAccounts": True, "properties": list(props),
        "series": [event(None, props=[APP])]}}


# Test builds report under "<app>-test" (docs/testing.md): out of every report.
# For insights it is a test-account filter (settings()); a HogQL tile adds it to
# each of its FROM events WHERE clauses.
REAL_PRODUCT = "not endsWith(coalesce(properties.product, ''), '-test')"


def leaves_test_products_out(query):
    """Whether every `from events` in a SQL tile carries REAL_PRODUCT (test_setup.py checks each tile)."""
    return query.count("from events") == query.count(REAL_PRODUCT) > 0


def sql(query, display="ActionsTable"):
    """A HogQL tile. Test products are left out; column names in Turkish are
    quoted: HogQL takes only ASCII bare."""
    query = query.replace("from events where ", f"from events where {REAL_PRODUCT} and ")
    query = re.sub(r"(?<![\w'`$])(\w*[^\x00-\x7f]\w*)", r"`\1`", query)
    return {"kind": "DataVisualizationNode", "display": display,
            "source": {"kind": "HogQLQuery", "query": query}}


def where_product(product):
    return f"and properties.product = '{product}'" if product else ""


# ---- tiles ------------------------------------------------------------------------
# A tile is (name, description, query, width, height); a str is a section header.


def live_tiles(product=None):
    p = where_product(product)
    return [
        ("Şu an açık", f"Son {LIVE_MINUTES} dakikada uygulaması çalışan makineler (heartbeat 5 dakikada bir gelir).",
         sql(f"select count(distinct person_id) from events where properties.channel = 'app' {p} "
             f"and timestamp > now() - interval {LIVE_MINUTES} minute", "BoldNumber"), 3, 3),
        ("Şu an sitede", "Son 5 dakikada sitede sayfa açan ziyaretçiler.",
         sql(f"select count(distinct person_id) from events where properties.channel = 'web' {p} "
             f"and event in ('$pageview', '$autocapture', '$pageleave') and timestamp > now() - interval 5 minute",
             "BoldNumber"), 3, 3),
    ]


def overview():
    return [
        *live_tiles(),
        ("Şu an açık, ürüne göre", "Her üründe şu an kaç makinede uygulama çalışıyor.",
         sql(f"select properties.product as ürün, count(distinct person_id) as makine from events "
             f"where properties.channel = 'app' and timestamp > now() - interval {LIVE_MINUTES} minute "
             f"group by ürün order by makine desc"), 6, 3),
        "## Kullanıcılar",
        ("Günlük aktif kullanıcı, uygulamalar", "Ürün başına, uygulamayı o gün çalıştıran tekil makine.",
         trends([event(None, "dau", [APP])], breakdown="product"), 6, 5),
        ("Günlük ziyaretçi, siteler", "Ürün sayfası başına tekil ziyaretçi; home ana sayfa, tools araç listesi.",
         trends([event("$pageview", "dau", [WEB])], breakdown="product"), 6, 5),
        ("Yeni kurulumlar", "first_launch: uygulamanın bir makinede ilk açılışı.",
         trends([event("first_launch", "total", [APP])], interval="week", display="ActionsBar",
                breakdown="product", days=90), 6, 5),
        ("Kullanıcı yaşam döngüsü, uygulamalar", "Yeni, geri dönen, yeniden canlanan ve kaybedilen kullanıcılar.",
         lifecycle(), 6, 5),
        "## Nereden",
        ("Ülkeler", "Uygulama ve site kullanıcıları, ülkeye göre.",
         trends([event(None, "dau")], display="WorldMap", breakdown="$geoip_country_code"), 6, 6),
        ("Şehirler", "", trends([event(None, "dau")], display="ActionsTable", breakdown="$geoip_city_name"), 6, 6),
        "## Siteden indirmeye",
        ("Ürün sayfasından indirmeye", "Ürün sayfasını açanların kaçı indirme düğmesine bastı ya da kurulum komutunu kopyaladı.",
         funnel([event("$pageview", props=[WEB]),
                 {"kind": "ActionsNode", "id": ACTIONS["Took a download step"]}], breakdown="product"), 12, 6),
        ("İndirme tıklamaları ve yeni kurulumlar", "Haftalık, ürüne göre: sitede indirme tıklaması ve "
         "uygulamanın ilk açılışı. Ziyaretçi ile uygulama farklı kişi sayılır; birebir değil, oran olarak okunur.",
         sql("select toStartOfWeek(timestamp) as hafta, properties.product as ürün, "
             "countIf(event = 'download_clicked') as indirme_tıklaması, countIf(event = 'first_launch') as yeni_kurulum "
             "from events where event in ('download_clicked', 'first_launch') and timestamp > now() - interval 90 day "
             "group by hafta, ürün order by hafta desc, ürün"), 12, 5),
    ]


def product_tiles(product):
    p = where_product(product)
    # In each query, not only the dashboard filter: alerts run the insight alone.
    only = {"key": "product", "value": product, "operator": "exact", "type": "event"}
    A, W = [APP, only], [WEB, only]
    return [
        *live_tiles(product),
        ("Kurulumlar (toplam)", "Bu ürünü hiç çalıştırmış tekil makine.",
         sql(f"select count(distinct person_id) from events where properties.channel = 'app' {p}", "BoldNumber"), 3, 3),
        ("Bugün kullanan", "", sql(f"select count(distinct person_id) from events where properties.channel = 'app' {p} "
                                   f"and toDate(timestamp) = today()", "BoldNumber"), 3, 3),
        "## Kullanım",
        ("Aktif kullanıcı: gün, hafta, ay", "",
         trends([event(None, "dau", A), event(None, "weekly_active", A), event(None, "monthly_active", A)]), 6, 5),
        ("Yeni kurulumlar", "", trends([event("first_launch", "total", A)], display="ActionsBar"), 6, 5),
        ("Kullanıcı başına günlük açık kalma (dk)", "heartbeat sürelerinin toplamı / o gün aktif makine. Çevrimdışı geçen süre de sonradan gelir.",
         trends([event("heartbeat", "sum", A, math_property="duration_s"), event("heartbeat", "dau", A)],
                formula="A/B/60"), 6, 5),
        ("Oturumlar ve ortalama oturum süresi", "Oturum: 30 dk boşluk ya da 24 saatte biter.",
         trends([event(None, "unique_session", A),
                 event(None, "avg", A, math_property="$session_duration", math_property_type="session_properties")]),
         6, 5),
        ("Yaşam döngüsü", "", lifecycle(A), 6, 5),
        ("Haftalık tutunma", "İlk açılıştan sonraki haftalarda geri gelenler.",
         retention("first_launch", "app_open", props=A), 6, 5),
        ("Bir çalışmada açık kalma süresi", "Uygulama açılıp kapanana kadar geçen süre (app_quit), dakika, son 30 gün.",
         sql(f"select properties.$app_version as sürüm, count() as kapanış, "
             f"round(quantile(0.5)(toFloat(properties.uptime_s)) / 60, 1) as medyan_dk, "
             f"round(quantile(0.9)(toFloat(properties.uptime_s)) / 60, 1) as p90_dk from events "
             f"where event = 'app_quit' {p} and timestamp > now() - interval 30 day "
             f"group by sürüm order by kapanış desc"), 6, 5),
        "## Ne yapılıyor",
        ("Ekranlar", "Hangi pencere ya da panel kaç kez açıldı ($screen).",
         trends([event("$screen", "total", A)], display="ActionsTable", breakdown="$screen_name"), 6, 6),
        ("Uygulamadaki eylemler", "Uygulamanın kendi gönderdiği event'ler; beacon'ın otomatikleri hariç.",
         sql(f"select event as eylem, count() as adet, count(distinct person_id) as makine from events "
             f"where properties.channel = 'app' {p} and event not in {tuple(SYSTEM_EVENTS)} "
             f"and timestamp > now() - interval 30 day group by eylem order by adet desc limit 50"), 6, 6),
        ("Son akış", "Bu ürünün son 100 event'i: makine, ekran, eylem.",
         sql(f"select timestamp as zaman, event, properties.$screen_name as ekran, properties.$app_version as sürüm, "
             f"properties.$os as os, properties.$geoip_city_name as şehir, distinct_id as makine from events "
             f"where properties.channel = 'app' {p} and event != 'heartbeat' order by timestamp desc limit 100"), 12, 7),
        "## Sürümler ve güncellemeler",
        ("Sürümlere göre aktif kullanıcı", "Her gün hangi sürümde kaç makine.",
         trends([event(None, "dau", A)], breakdown="$app_version", display="ActionsAreaGraph"), 6, 5),
        ("Güncelleme başarısı", "update_started'dan sonra yeni sürüm açıldı mı (app_updated), 1 gün içinde.",
         funnel([event("update_started", props=A), event("app_updated", props=A)], window_days=1), 6, 5),
        ("Başarısız güncellemeler", "Adım ve hataya göre.",
         sql(f"select properties.step as adım, properties.error as hata, properties.kind as tür, "
             f"properties.$os as os, count() as adet, max(timestamp) as son from events "
             f"where event = 'update_failed' {p} and timestamp > now() - interval 30 day "
             f"group by adım, hata, tür, os order by adet desc"), 12, 4),
        ("Atlanan sürümler", "Kullanıcının \"skip\" dediği sürümler; çok atlanan bir sürüm bir şey söylüyor.",
         sql(f"select properties.to as sürüm, count(distinct person_id) as makine, max(timestamp) as son from events "
             f"where event = 'update_skipped' {p} and timestamp > now() - interval 90 day "
             f"group by sürüm order by sürüm desc"), 12, 3),
        "## Makineler",
        ("İşletim sistemi", "", trends([event(None, "dau", A)], display="ActionsPie", breakdown="$os"), 4, 5),
        ("OS sürümü", "", trends([event(None, "dau", A)], display="ActionsTable", breakdown="$os_version"), 4, 5),
        ("Mimari", "", trends([event(None, "dau", A)], display="ActionsPie", breakdown="arch",
                              breakdown_type="person"), 4, 5),
        ("Model", "", trends([event(None, "dau", A)], display="ActionsTable", breakdown="$device_model"), 4, 5),
        ("Bellek (GB)", "", trends([event(None, "dau", A)], display="ActionsTable", breakdown="memory_gb",
                                   breakdown_type="person"), 4, 5),
        ("Ekran", "Ana ekranın boyutu (nokta) ve ekran sayısı.",
         trends([event(None, "dau", A)], display="ActionsTable",
                breakdown="concat(toString(properties.$screen_width), '×', toString(properties.$screen_height), "
                          "' / ', toString(person.properties.screen_count))", breakdown_type="hogql"), 4, 5),
        ("Dil", "", trends([event(None, "dau", A)], display="ActionsTable", breakdown="$locale"), 4, 5),
        ("Ülke", "", trends([event(None, "dau", A)], display="WorldMap", breakdown="$geoip_country_code"), 8, 5),
        "## Ürün sayfası (site)",
        ("Sayfa ziyaretçileri", "", trends([event("$pageview", "dau", W)]), 6, 5),
        ("İndirmeler", "İndirme düğmesi (platforma göre) ve kurulum komutu kopyalama.",
         trends([event("download_clicked", "total", W), event("install_command_copied", "total", W)],
                breakdown="platform", display="ActionsBar"), 6, 5),
    ]


def live():
    return [
        *live_tiles(),
        ("Şu an kimde ne açık", "Makine başına şu an çalışan uygulamalar, son görülen ekran ve yer.",
         sql(f"select distinct_id as makine, arrayStringConcat(groupUniqArray(properties.product), ', ') as uygulamalar, "
             f"argMax(properties.$screen_name, if(event = '$screen', timestamp, toDateTime(0))) as son_ekran, "
             f"any(properties.$os) as os, any(properties.$geoip_city_name) as şehir, "
             f"any(properties.$geoip_country_code) as ülke, max(timestamp) as son from events "
             f"where properties.channel = 'app' and timestamp > now() - interval {LIVE_MINUTES} minute "
             f"group by makine order by son desc"), 12, 6),
        ("Sitede şu an", "Son 5 dakikada açık sayfalar.",
         sql("select properties.$pathname as sayfa, properties.product as ürün, count(distinct person_id) as ziyaretçi, "
             "max(timestamp) as son from events where properties.channel = 'web' and event = '$pageview' "
             "and timestamp > now() - interval 5 minute group by sayfa, ürün order by ziyaretçi desc"), 12, 4),
        ("Son 24 saat, saatlik", "Uygulama başına saatlik aktif makine.",
         trends([event(None, "dau", [APP])], days=1, interval="hour", breakdown="product"), 12, 5),
        ("Son akış", "Tüm ürünlerden son 100 event (heartbeat hariç).",
         sql("select timestamp as zaman, properties.channel as kanal, properties.product as ürün, event, "
             "coalesce(properties.$screen_name, properties.$pathname) as ekran_sayfa, properties.$geoip_city_name as şehir, "
             "distinct_id as kişi from events where event not in ('heartbeat', '$set') "
             "order by timestamp desc limit 100"), 12, 8),
    ]


def websites():
    return [
        live_tiles()[1],
        ("Bugünkü ziyaretçi", "", sql("select count(distinct person_id) from events where properties.channel = 'web' "
                                      "and toDate(timestamp) = today()", "BoldNumber"), 3, 3),
        "## Trafik",
        ("Ziyaretçi ve sayfa görüntüleme", "",
         trends([event("$pageview", "dau", [WEB]), event("$pageview", "total", [WEB])], compare=True), 6, 5),
        ("Sitelere göre ziyaretçi", "apps ve portföy.", trends([event("$pageview", "dau", [WEB])], breakdown="site",
                                                                display="ActionsLineGraph"), 6, 5),
        ("Ürün sayfalarına göre ziyaretçi", "", trends([event("$pageview", "dau", [WEB])], breakdown="product",
                                                        display="ActionsBar"), 6, 5),
        ("En çok görüntülenen sayfalar", "", trends([event("$pageview", "total", [WEB])], display="ActionsTable",
                                                     breakdown="$pathname"), 6, 6),
        ("Nereden geliyorlar", "Yönlendiren site.", trends([event("$pageview", "dau", [WEB])], display="ActionsTable",
                                                           breakdown="$referring_domain"), 6, 6),
        "## Dönüşüm",
        ("İndirme tıklamaları", "Ürün ve platforma göre.",
         trends([event("download_clicked", "total", [WEB])], display="ActionsTable",
                breakdown="concat(properties.product, ' / ', properties.platform)", breakdown_type="hogql"), 6, 5),
        ("Kopyalanan kurulum komutları", "", trends([event("install_command_copied", "total", [WEB])],
                                                     display="ActionsTable", breakdown="command"), 6, 5),
        ("Dışarı giden linkler", "", trends([event("outbound_link_clicked", "total", [WEB])], display="ActionsTable",
                                            breakdown="href"), 12, 5),
        "## Ziyaretçiler",
        ("Ülkeler", "", trends([event("$pageview", "dau", [WEB])], display="WorldMap",
                               breakdown="$geoip_country_code"), 6, 5),
        ("Cihaz ve tarayıcı", "", trends([event("$pageview", "dau", [WEB])], display="ActionsTable",
                                         breakdown="concat(properties.$device_type, ' / ', properties.$browser, ' / ', properties.$os)",
                                         breakdown_type="hogql"), 6, 5),
        ("Sayfa hızı (LCP, p75, ms)", "Web vitals: büyük içeriğin yüklenme süresi.",
         trends([event("$web_vitals", "p75", [WEB], math_property="$web_vitals_LCP_value")], breakdown="$pathname",
                display="ActionsTable"), 12, 5),
    ]


def updates():
    return [
        "## Sürümler",
        ("Ürün ve sürüme göre aktif makine", "Son 7 gün.",
         trends([event(None, "dau", [APP])], days=7, display="ActionsTable",
                breakdown="concat(properties.product, ' ', properties.$app_version)", breakdown_type="hogql"), 6, 6),
        ("Güncelleme başarısı, ürüne göre", "",
         funnel([event("update_started", props=[APP]), event("app_updated", props=[APP])], window_days=1,
                breakdown="product"), 6, 6),
        ("Başarısız güncellemeler", "",
         sql("select properties.product as ürün, properties.step as adım, properties.error as hata, "
             "properties.kind as tür, properties.$os as os, count() as adet, max(timestamp) as son from events "
             "where event = 'update_failed' and timestamp > now() - interval 30 day "
             "group by ürün, adım, hata, tür, os order by adet desc"), 12, 5),
        ("Güncellemeler ve yeni kurulumlar", "",
         trends([event("app_updated", "total", [APP]), event("first_launch", "total", [APP])], display="ActionsBar",
                interval="week", days=90), 12, 5),
    ]


EXC_TYPE = "JSONExtractString(properties.$exception_list, 1, 'type')"
EXC_VALUE = "JSONExtractString(properties.$exception_list, 1, 'value')"


def health():
    return [
        guide("Hatalar ve sağlık", [
            "`$exception`: uygulamanın yakaladığı ve yakalayamadığı hatalar, macOS'un yazdığı çökme raporları.",
            "`unclean_exit`: uygulama önceki sefer düzgün kapanmadı (çöktü ya da öldürüldü).",
            f"Hataların tek tek ayrıntısı ve stack'leri: [Error tracking]({UI}/error_tracking).",
            "Alarmlar e-postayla gelir: başarısız güncellemeler, hata artışı, çökmeler; ürün sessiz kalırsa.",
            "Kullanıcıdan log istemek için: uygulamanın veri klasöründeki `<uygulama>.log` ve `<uygulama>.crash.log`."]),
        ("Hatalar (24 saat)", "", sql("select count() from events where event = '$exception' "
                                      "and timestamp > now() - interval 1 day", "BoldNumber"), 3, 3),
        ("Çökme / zorla kapanma (7 gün)", "unclean_exit.",
         sql("select count() from events where event = 'unclean_exit' and timestamp > now() - interval 7 day",
             "BoldNumber"), 3, 3),
        ("Başarısız güncelleme (7 gün)", "",
         sql("select count() from events where event = 'update_failed' and timestamp > now() - interval 7 day",
             "BoldNumber"), 3, 3),
        ("Hata alan makine (7 gün)", "", sql("select count(distinct person_id) from events where event = '$exception' "
                                             "and timestamp > now() - interval 7 day", "BoldNumber"), 3, 3),
        "## Eğilim",
        ("Hatalar, günlük", "Çökme raporları dahil. Alarm: günde 20'den fazla.", trends([event("$exception", "total", [APP])]), 6, 5),
        ("Çökmeler, günlük", "unclean_exit (crash raporları Hatalar'da da sayılır).",
         trends([event("unclean_exit", "total", [APP])]), 6, 5),
        ("Başarısız güncellemeler, günlük", "Alarm: günde 3'ten fazla.",
         trends([event("update_failed", "total", [APP])]), 6, 5),
        ("Hatalar, ürüne göre", "", trends([event("$exception", "total", [APP])], breakdown="product",
                                           display="ActionsBar"), 6, 5),
        "## Kararlılık",
        ("Çökmesiz kullanıcı, sürüme göre", "Son 30 gün: o sürümü kullanan makinelerin yüzde kaçı hiç çökmedi "
         "(unclean_exit ya da macOS çökme raporu). Çökme, çöken sürüme yazılır.",
         sql("select ürün, sürüm, count(distinct if(tip = 'aktif', kişi, null)) as kullanıcı, "
             "count(distinct if(tip = 'çöktü', kişi, null)) as çöken, "
             "round(100 * (1 - count(distinct if(tip = 'çöktü', kişi, null)) / "
             "greatest(count(distinct if(tip = 'aktif', kişi, null)), 1)), 1) as çökmesiz_yüzde from ("
             "select properties.product as ürün, properties.$app_version as sürüm, person_id as kişi, 'aktif' as tip "
             "from events where event in ('app_open', 'heartbeat', 'daily_active') and timestamp > now() - interval 30 day "
             "union all "
             "select properties.product as ürün, coalesce(properties.version, properties.crashed_version) as sürüm, "
             "person_id as kişi, 'çöktü' as tip from events where (event = 'unclean_exit' or "
             "(event = '$exception' and properties.crash_report = true)) and timestamp > now() - interval 30 day"
             ") group by ürün, sürüm order by ürün, sürüm desc"), 12, 5),
        "## En sık",
        ("En sık hatalar", "Son 7 gün: tür, mesaj, ürün ve sürüm.",
         sql(f"select {EXC_TYPE} as tür, {EXC_VALUE} as mesaj, properties.product as ürün, "
             f"properties.$app_version as sürüm, properties.$exception_level as seviye, count() as adet, "
             f"count(distinct person_id) as makine, max(timestamp) as son from events "
             f"where event = '$exception' and timestamp > now() - interval 7 day "
             f"group by tür, mesaj, ürün, sürüm, seviye order by adet desc limit 50"), 12, 6),
        ("Çökmeler, sürüme göre", "Hangi sürüm kaç kez düzgün kapanmadı.",
         sql("select properties.product as ürün, properties.version as sürüm, properties.$os as os, count() as adet, "
             "count(distinct person_id) as makine, max(timestamp) as son from events where event = 'unclean_exit' "
             "and timestamp > now() - interval 30 day group by ürün, sürüm, os order by adet desc"), 12, 5),
    ]


# (dashboard, tile, alert name, bounds, interval, enabled). An alert runs its
# insight on its own, so the tile's query must hold every filter it needs.
# The "sessiz" ones stay off until the product is in LIVE. The free
# plan allows 5 alerts: "Hatalar" covers crashes too (macOS crash reports
# arrive as $exception).
ALERT_LIMIT = 5


def alerts():
    out = [
        ("Hatalar ve sağlık", "Hatalar, günlük", "Hatalar arttı", {"upper": 20}, "daily", True),
        ("Hatalar ve sağlık", "Başarısız güncellemeler, günlük", "Güncellemeler başarısız oluyor",
         {"upper": 3}, "daily", True),
    ]
    for title in PRODUCTS.values():
        out.append((title, "Aktif kullanıcı: gün, hafta, ay", f"{title} sessiz: bugün hiç kullanıcı yok",
                    {"lower": 1}, "daily", title in {PRODUCTS[p] for p in LIVE}))
    return out


def guide(title, lines):
    return f"### {title}\n\n" + "\n".join(f"- {line}" for line in lines)


LINKS = [
    "[Şu an](LIVE) ekranı: kimde ne açık, canlı akış",
    f"[Web analytics]({UI}/web): PostHog'un hazır site raporu",
    f"[Session replay]({UI}/replay/home): site ziyaretlerinin kaydı",
    f"[Heatmaps]({UI}/heatmaps): sitede nereye tıklanıyor",
    f"[Activity]({UI}/activity/explore): tek tek event'ler ve tüm alanları",
    f"[Persons]({UI}/persons): makineler ve özellikleri",
]


def dashboards():
    """name -> (description, tiles, dashboard filter or None)."""
    out = {
        "Genel bakış": ("Tüm ürünler, uygulama ve site birlikte.", [guide("Genel bakış", [
            "Her kart ürüne (product) ve kanala (app / web) göre ayrılır.",
            "Tek ürüne bakmak için o ürünün dashboard'una geç ya da üstteki filtreden `product` seç.",
            *LINKS])] + overview(), None),
        "Şu an": ("Canlı: şu an açık uygulamalar ve sitedeki ziyaretçiler. Sağ üstten otomatik yenilemeyi aç.",
                  live(), None),
        "Web siteleri": ("Siteler: trafik, dönüşüm, ziyaretçiler, hız.", websites(), None),
        "Güncellemeler": ("Tüm ürünlerde sürümler ve self-update sağlığı.", updates(), None),
        "Hatalar ve sağlık": ("Hatalar, çökmeler, başarısız güncellemeler ve alarmları.", health(), None),
    }
    for product, title in PRODUCTS.items():
        out[title] = (f"{title}: uygulama ve ürün sayfası.", product_tiles(product),
                      [{"key": "product", "value": product, "operator": "exact", "type": "event"}])
    return out


# ---- API ------------------------------------------------------------------------


def call(method, path, body=None):
    request = urllib.request.Request(
        path if path.startswith("https://") else API + path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + os.environ["POSTHOG_KEY"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path}: {e.code} {e.read().decode()[:800]}")


def run_query(name, query):
    source = query["source"]
    result = call("POST", "/query/", {"query": source})
    if result.get("error"):
        raise SystemExit(f"{name}: {result['error']}")


def tiles_with_layout(tiles):
    """Rows of tiles 12 wide; a header takes a full row."""
    x = y = row = 0
    for tile in tiles:
        if isinstance(tile, str):  # a section header, or a guide (longer)
            w, h = 12, 1 if tile.startswith("## ") else 3
        else:
            w, h = tile[3], tile[4]
        if x + w > 12:
            x, y, row = 0, y + row, 0
        yield tile, {"x": x, "y": y, "w": w, "h": h}
        x, row = x + w, max(row, h)


def owned(current, titles, retired):
    """The tiles on a dashboard this script made: its text tiles, and insights
    named like one of its tiles (now, or before a rename: RETIRED). An insight
    someone added by hand has another name and stays."""
    return [t for t in current if t.get("text") or (t.get("insight") or {}).get("name") in titles | retired]


def build(name, description, tiles, filters, existing):
    dashboard = existing.get(name)
    if dashboard is None:
        dashboard = call("POST", "/dashboards/", {"name": name})
    did = dashboard["id"]
    titles = {t[0] for t in tiles if not isinstance(t, str)}
    old = owned(call("GET", f"/dashboards/{did}/").get("tiles") or [], titles, RETIRED)
    call("PATCH", f"/dashboards/{did}/", {
        "description": description, "pinned": True, "filters": {"properties": filters or []}})
    # The new tiles first, the old ones after: a run that fails half way leaves
    # the dashboard showing what it showed (plus some new tiles), never half empty.
    layout = []
    for tile, place in tiles_with_layout(tiles):
        if isinstance(tile, str):
            before = {t["id"] for t in call("GET", f"/dashboards/{did}/").get("tiles") or []}
            r = call("PATCH", f"/dashboards/{did}/", {"tiles": [{"text": {"body": tile}, "color": None}]})
            layout.append((max(t["id"] for t in r["tiles"] if t["id"] not in before), place))
        else:
            title, about, query, _, _ = tile
            insight = call("POST", "/insights/", {"name": title, "description": about, "query": query,
                                                  "dashboards": [did]})
            tid = next((t["id"] for t in insight.get("dashboard_tiles") or [] if t.get("dashboard_id") == did), None)
            if tid is None:
                fresh = call("GET", f"/dashboards/{did}/")
                tid = next(t["id"] for t in fresh["tiles"] if (t.get("insight") or {}).get("id") == insight["id"])
            layout.append((tid, place))
    for tile in old:  # what this script made last time (insights are soft-deleted)
        if tile.get("insight"):
            call("PATCH", f"/insights/{tile['insight']['id']}/", {"deleted": True})
    if old:
        call("PATCH", f"/dashboards/{did}/", {"tiles": [{"id": t["id"], "deleted": True} for t in old]})
    call("PATCH", f"/dashboards/{did}/", {"tiles": [{"id": tid, "layouts": {"sm": place}} for tid, place in layout]})
    return did


def set_alerts(ids):
    """Rebuilds the alerts this script owns (by name) on the insights it just made."""
    me = call("GET", "https://eu.posthog.com/api/users/@me/")["id"]
    wanted = alerts()
    names = {a[2] for a in wanted}
    for alert in call("GET", "/alerts/?limit=200")["results"]:
        if alert["name"] in names:
            call("DELETE", f"/alerts/{alert['id']}/")
    for dashboard, tile, name, bounds, interval, enabled in wanted:
        tiles = call("GET", f"/dashboards/{ids[dashboard]}/")["tiles"]
        insight = next(t["insight"]["id"] for t in tiles if (t.get("insight") or {}).get("name") == tile)
        call("POST", "/alerts/", {
            "name": name, "insight": insight, "subscribed_users": [me], "enabled": enabled,
            "threshold": {"configuration": {"type": "absolute", "bounds": bounds}},
            "condition": {"type": "absolute_value"}, "calculation_interval": interval,
            "config": {"type": "TrendsAlertConfig", "series_index": 0}})


def digest(ids):
    """The overview by e-mail every Monday morning, to the owner of the API key."""
    me = call("GET", "https://eu.posthog.com/api/users/@me/")
    title = "Haftalık özet"
    for sub in call("GET", "/subscriptions/?limit=100")["results"]:
        if sub.get("title") == title and not sub.get("deleted"):
            call("PATCH", f"/subscriptions/{sub['id']}/", {"deleted": True})
    tiles = call("GET", f"/dashboards/{ids['Genel bakış']}/")["tiles"]
    insights = [t["insight"]["id"] for t in tiles if t.get("insight")]
    call("POST", "/subscriptions/", {
        "title": title, "dashboard": ids["Genel bakış"], "dashboard_export_insights": insights,
        "target_type": "email", "target_value": me["email"],
        "frequency": "weekly", "interval": 1, "byweekday": ["monday"], "start_date": "2026-11-23T06:00:00Z"})


def settings():
    call("PATCH", "", {
        "name": "Uygulamalar ve siteler",
        "timezone": "Europe/Istanbul",
        "anonymize_ips": True,  # the sites' IPs; the apps' never reach PostHog
        "app_urls": list(SITES.values()),  # where the toolbar and heatmaps may open
        "session_recording_opt_in": True,
        "heatmaps_opt_in": True,
        "autocapture_web_vitals_opt_in": True,
        "capture_performance_opt_in": True,
        "capture_console_log_opt_in": True,
        "capture_dead_clicks": True,
        "autocapture_exceptions_opt_in": True,
        "test_account_filters_default_checked": True,
        "test_account_filters": [
            {"key": "$host", "type": "event", "value": "^(localhost|127\\.0\\.0\\.1)(:\\d+)?$", "operator": "not_regex"},
            # Test builds: "<app>-test" (docs/testing.md). SQL tiles add REAL_PRODUCT themselves.
            {"key": "product", "type": "event", "value": "-test$", "operator": "not_regex"},
        ],
    })


def actions():
    wanted = {
        "Took a download step": [{"event": "download_clicked"}, {"event": "install_command_copied"}],
        "Uygulama başlatıldı": [{"event": "app_open"}, {"event": "first_launch"}],
    }
    existing = {a["name"]: a for a in call("GET", "/actions/?limit=200")["results"]}
    for name, steps in wanted.items():
        body = {"name": name, "steps": steps}
        if name in existing:
            ACTIONS[name] = call("PATCH", f"/actions/{existing[name]['id']}/", body)["id"]
        else:
            ACTIONS[name] = call("POST", "/actions/", body)["id"]


def cohorts():
    def performed(event_name, days):
        return {"properties": {"type": "OR", "values": [{"type": "AND", "values": [
            {"key": event_name, "type": "behavioral", "value": "performed_event", "event_type": "events",
             "time_value": days, "time_interval": "day"}]}]}}

    wanted = {
        "Uygulama kullananlar (7 gün)": performed("heartbeat", 7),
        "Uygulama kullananlar (30 gün)": performed("heartbeat", 30),
        "Site ziyaretçileri (30 gün)": performed("$pageview", 30),
    }
    existing = {c["name"]: c for c in call("GET", "/cohorts/?limit=200")["results"]}
    for name, body in wanted.items():
        body = {"name": name, "is_static": False, "filters": body}
        if name in existing:
            call("PATCH", f"/cohorts/{existing[name]['id']}/", body)
        else:
            call("POST", "/cohorts/", body)


def main(argv):
    if "--check" in argv:
        ACTIONS.update({a["name"]: a["id"] for a in call("GET", "/actions/?limit=200")["results"]})
    else:
        actions()
    wanted = dashboards()
    for name, (_, tiles, _) in wanted.items():
        for tile in tiles:
            if not isinstance(tile, str):
                run_query(f"{name} / {tile[0]}", tile[2])
    print(f"{sum(1 for _, t, _ in wanted.values() for x in t if not isinstance(x, str))} queries run without errors")
    if "--check" in argv:
        return
    settings()
    existing = {d["name"]: d for d in call("GET", "/dashboards/?limit=200")["results"] if not d.get("deleted")}
    ids = {name: build(name, *spec, existing) for name, spec in wanted.items()}
    # Live view links point at the dashboard by id; fill them in now that it exists.
    first = wanted["Genel bakış"][1][0].replace("(LIVE)", f"({UI}/dashboard/{ids['Şu an']})")
    current = call("GET", f"/dashboards/{ids['Genel bakış']}/")
    guide_tile = next(t for t in current["tiles"] if (t.get("text") or {}).get("body", "").startswith("### Genel"))
    call("PATCH", f"/dashboards/{ids['Genel bakış']}/", {"tiles": [{"id": guide_tile["id"], "text": {"body": first}}]})
    call("PATCH", "", {"primary_dashboard": ids["Genel bakış"]})
    for old in ("Uygulamalar", "Web sitesi"):  # replaced by the ones above
        if old in existing and old not in wanted:
            call("PATCH", f"/dashboards/{existing[old]['id']}/", {"pinned": False})
    cohorts()
    set_alerts(ids)
    digest(ids)
    for name, did in ids.items():
        print(f"{did}  {name}  {UI}/dashboard/{did}")


if __name__ == "__main__":
    main(sys.argv[1:])
