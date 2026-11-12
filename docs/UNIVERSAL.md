# Universal versiya: Windows + Linux (2026-11-11)

Bot bir koddur (`src/`), iki "qabıqla" işləyir. Hər iki tərəfdə eyni Python
(3.12), eyni `requirements.txt` və eyni STOP/HALT mənası var.

## İdarəetmə əmrləri

| Əməliyyat | Windows | Linux server |
|---|---|---|
| Başlat | `start_bot.bat` | `./start_bot.sh` |
| Pauza (yeni əmr yox, pozisiyalar idarə olunur) | `pause_bot.bat` | `./pause_bot.sh` |
| Tam dayandır | `stop_bot.bat` | `./stop_bot.sh` |
| HALT-dan sonra yenidən başlat | `start_bot.bat force` | `./start_bot.sh force` |

## STOP faylının mənası (hər iki sistemdə eyni)

- **Boş `STOP`** = pauza. Bot işləyir, yeni əmr açmır, açıq pozisiyaları idarə edir.
  `start_bot` onu silir (davam).
- **İçində `HALT` olan `STOP`** = tam dayanma. Avtomatik başlatma (Task Scheduler,
  Docker restart) onu **silmir**; yalnız `force` ilə əl ilə başlatmaq olar.
- **`data/supervisor.stop`** = supervisor botu yenidən başlatmır.

## Harada işlədiyini seçmək (`runtime.location`)

`config.yaml`-dakı `runtime.location` botun **hansı maşında** işləyəcəyini deyir
(`local` = Windows kompüter, `server` = Linux server). Server konteyneri özünü
`BOT_LOCATION=server` kimi tanıdır. Serverə keçəndə `runtime.location: server`
edib push edirsən: Windows-dakı bot artıq başlamır, server başlayır. Bu, eyni
hesabda iki botun eyni anda işləməsinin qarşısını alır.

## Linux tərəfi

MT5 və bot Wine içində işləyir (Linux üçün MT5 yoxdur). Konteyner Python 3.12.10
qurur və paketləri `requirements.txt`-dən quraşdırır; fayl dəyişəndə yenidən
qurur. Ətraflı: `docs/SERVER.md`.

## Avtomatik yoxlama

`.github/workflows/tests.yml` hər push-da testləri Windows və Linux-da işlədir.

## Panel (dashboard) və telefon

- Yuxarıda: botun vəziyyəti, hesab növü (DEMO/REAL), harada işlədiyi (Windows/Linux),
  MT5 bağlantısı, nəzarətçi və beynin statusu.
- Əsas tabda: balans, bu günkü nəticə (kəşf əməliyyatları və açıq mövqelər daxil),
  hər əmrdə risk (% və $), gündəlik və həftəlik zərər limitinin nə qədər istifadə olunduğu.
- **Mövqe qrafikləri**: hər açıq mövqe üçün şam qrafiki (M1 · 2 saat və ya M5 · 10 saat), üzərində giriş, SL, TP və cari qiymət xətləri, nəticə $ və R ilə. Bot qrafik datasını hər 15 saniyədən bir `data/charts/` qovluğuna yazır (`dashboard.chart_refresh_seconds`).
- **📱 Telefon görünüşü**: cədvəllərdə yalnız əsas sütunlar, daha az yenilənmə (10 san).
  Ünvan `?view=phone` ilə açılır; onu telefonda əlfəcinə və ya əsas ekrana əlavə et.
- Serverdə panel yalnız `127.0.0.1:8501`-də açılır (internetə açıq deyil). Telefondan
  baxmaq üçün təhlükəsiz yol: serverə və telefona **Tailscale** quraşdırıb şəxsi şəbəkə
  ilə qoşulmaq. Hər halda `.env`-də `DASHBOARD_PASSWORD` təyin et; parol yoxdursa panel
  serverdə xəbərdarlıq göstərir.

## Ehtiyat nüsxə

Bot hər UTC günü `data/bot.db`-ni `data/backups/bot-YYYY-MM-DD.db` kimi kopyalayır
(son 7 gün saxlanılır, `backup.keep_days`).

## Server skriptləri

`scripts/server.ps1` və `scripts/deploy.ps1` server ünvanını artıq repodan yox, kompüterdən oxuyur:

```
setx TRADINGBOT_SERVER "ec2-user@<server-ip>"
```

Real hesaba keçid meyarları: [REAL_ACCOUNT_CHECKLIST.md](REAL_ACCOUNT_CHECKLIST.md).

## Ticarət planı (2026-11-12)

- **Alətlər:** yalnız GOLD, US500, NAS100, US30 (forex çıxarıldı: qısa hərəkətlərdə spred 15–30% yeyirdi).
- **Saatlar** (yalnız yeni əmrlər; açıq mövqelər hər zaman idarə olunur, `universe.trading_hours`):
  - US500, NAS100, US30: Nyu-York 09:30–16:00 → Bakı qışda 18:30–01:00, yayda 17:30–00:00.
  - GOLD: Nyu-York 04:00–17:00 → Bakı qışda 13:00–02:00, yayda 12:00–01:00.
- **Təkrar əmr qaydaları söndürülüb:** 20 dəq gözləmə yoxdur, alətə gündəlik limit yoxdur, 3 zərərdən sonra
  24 saat fasilə yoxdur, eyni alətdə bir neçə mövqe ola bilər. Ümumi qorumalar qalır: eyni anda ən çoxu
  3 mövqe, gündəlik 10% və həftəlik 25% zərər limiti, 6% düşüşdə ümumi fasilə.
- **Əmr:** SL/TP 30 dəqiqəlik hərəkətə görə (M1 ATR × √30), TP = 1.5 × risk.
  Stagnation 10 dəq, maksimum saxlama 30 dəq. Demo-da risk 10$ (1000$-ın 1%-i) → TP ≈ +15$.
- **ABŞ indeksləri bir qrupdur:** eyni anda ən çoxu 2 indeks mövqeyi (`max_positions_per_underlying_group`).
- **Həftəsonu ticarəti söndürülüb.**
