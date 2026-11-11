# Real hesaba keçid meyarları

Qərar emosiyaya görə yox, rəqəmlərə görə verilsin. Hamısı **demo** hesabda,
**xərclərdən sonra** yoxlanır. Bir bənd də ödənmirsə, real hesaba keçilmir.

## Demo nəticələri
- [ ] Ən azı **4 həftə** fasiləsiz işləyib (həftəsonları daxil).
- [ ] Ən azı **100 bağlanmış əməliyyat**.
- [ ] Ümumi nəticə xərclərdən sonra **müsbətdir** (profit factor ≥ 1.2).
- [ ] Maksimum düşüş (drawdown) **10%-dən azdır**.
- [ ] Heç bir həftə **-5%-dən pis** bitməyib.
- [ ] Nəticə bir alət və ya bir gündən gəlmir: ən yaxşı alət çıxarılanda da müsbətdir.
- [ ] Beyin aktivdirsə: `data/brain/gate.json` = PASS və canlı sapma qoruyucusu işə düşməyib.

## Texniki yoxlama
- [ ] Bot 4 həftədə özbaşına dayanmayıb və ya dayananda Telegram xəbər verib.
- [ ] `stop_bot`, `pause_bot`, `start_bot force` sınanıb və gözlənildiyi kimi işləyir.
- [ ] Gündəlik ehtiyat nüsxələr `data/backups/` qovluğunda yaranır.
- [ ] `.env`-də `DASHBOARD_PASSWORD` və Telegram tənzimlənib.
- [ ] GitHub reposu **Private**-dir.

## Real hesabın özü
- [ ] Kiçik balans (məsələn 50$) üçün hesab növü **XM Micro**-dur (adi hesabda 0.25% minimum lota çatmır).
- [ ] `config.yaml`: `account.mode: real`, `.env`: `ALLOW_REAL=true` — ikisi də bilərəkdən, son addım kimi.
- [ ] İlk 2 həftə real nəticəni demo ilə müqayisə et: spred/slippage fərqi böyükdürsə, dayan.
