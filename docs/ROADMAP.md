# Etapy rozwoju

1. **Market Data (zrealizowany):** publiczne OHLCV, tickery i bieżący order book przez CCXT REST; UTC; walidacja świec i jakości; paginacja; cache; kontrolowane uzupełnianie luk; synchronizacja SQLite; CLI i fixture’y offline. WebSocket OHLCV jest opcjonalny i zależny od CCXT Pro oraz możliwości giełdy. Snapshot order booka nie zapewnia historycznej płynności.
2. **Fundament analityczny (zrealizowany):** wskaźniki, reżim, regułowe scenariusze i wielointerwałowy Strategy Engine.
3. **Rzetelna walidacja strategii (zrealizowana):** chronologiczny backtest bez look-ahead, prowizje i poślizg, metryki, eksport raportów i testy deterministyczne.
4. **Paper trading i zarządzanie ryzykiem (zrealizowany moduł):** trwały portfel SQLite, niezależna bramka ryzyka, sizing, dziennik sygnałów/fill’i oraz kontrola ekspozycji. Automatyczny scheduler/podłączenie strumienia danych pozostaje do integracji.
5. **Interfejsy:** rozszerzenie API o paper/backtest oraz dashboard z aktualnością danych, wynikami i błędami źródeł.
6. **Powiadomienia i obserwowalność:** opcjonalny Telegram, logowanie, monitoring i audyt.
7. **Opcjonalne ML/LLM:** wyłącznie opis/analiza wejściowa z uziemieniem w danych; bez prawa do omijania limitów ani wysyłania zleceń.
8. **Przyszła integracja egzekucji:** osobny, jawnie włączany etap z kontrolami i testami; nie jest częścią obecnego trybu.
