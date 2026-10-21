# Trading Agent

Trading Agent analizuje rynki spot BTC, ETH, XRP i SOL na podstawie publicznych danych OHLCV. Pobiera też tickery (ostatnia cena, bid/ask i wolumen, jeżeli są dostępne), kontroluje jakość oraz przechowuje świeczki i pochodzenie pobrań w SQLite. Obecna analiza pozostaje regułowa: EMA12/EMA26, RSI14 i ATR14, reżim rynku oraz warunkowe scenariusze.

Domyślny tryb to **analysis only**. Program nie posiada kluczy giełdowych, nie składa zleceń i nie zarządza środkami. Sygnał jest regułowym scenariuszem, a nie prognozą ani poradą inwestycyjną. Wynik ma ocenę jakości, poziom unieważnienia/stop, cele 2R i 3R oraz szacowany koszt. Spread jest pobierany z bid/ask, gdy są dostępne; zakres świecy jest opisanym przybliżeniem awaryjnym.

## Wymagania

Python 3.11 lub nowszy. Do podstawowej analizy potrzebne jest połączenie z Internetem i publiczny dostęp wybranej giełdy. Analiza i synchronizacja danych nie wymagają klucza API. Dashboard/API i testy używają opcjonalnych zestawów zależności opisanych niżej. SQLite jest wbudowane w obsługiwane wydania Pythona, nie wymaga serwera bazy danych.

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,api]"
Copy-Item .env.example .env
tradingagent --check-config
tradingagent --check-connection
tradingagent
```

### Linux / Fedora

```bash
python3 --version  # Python 3.11+; Python 3.14 jest obsługiwany przez użyte pakiety
python3 -m venv .venv
source .venv/bin/activate
.venv/bin/python -m pip --version
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev,api]'
cp .env.example .env
tradingagent --check-config
tradingagent --check-connection
tradingagent
```

`sudo dnf install python3 python3-pip` instaluje wymagane narzędzia w typowej instalacji Fedory. Używaj pip z aktywnego `.venv` (albo jawnej ścieżki `.venv/bin/python -m pip`), nie instaluj zależności globalnie. `sqlite` jest potrzebne tylko do opisanych niżej poleceń kopii i kontroli bazy przez konsolę. W podstawowym trybie bez dashboardu można zainstalować tylko `python -m pip install -e .`; testy wymagają dodatku `[dev]`, API/dashboard dodatku `[api]`. Zestaw `[dev,api]` instaluje pytest, FastAPI, Uvicorn oraz klienta testowego Starlette. ML, LLM i Telegram nie dodają zależności pip.

Aplikacja odczytuje ustawienia ze zmiennych środowiskowych i opcjonalnie z lokalnego `.env` (zmienne procesu mają pierwszeństwo). Nie commituj `.env`; jest ignorowany przez Git. Używa publicznych endpointów, które zwykle nie wymagają klucza API. Dostępność par, ograniczenia giełdy i regionu mogą się różnić. Błąd źródła lub brak rynku nie są zastępowane fikcyjnymi danymi. `tradingagent --check-config` waliduje konfigurację. Tryb `live` jest blokowany. Paper trading działa wyłącznie w lokalnym portfelu symulowanym.

## Architektura i dane

Kod znajduje się w `src/tradingagent/`. Domyślne interwały to 5m, 15m, 1h, 4h i 1d; giełda i rynek muszą je faktycznie udostępniać. Normalizator oznacza świecę jako kompletną dopiero po upływie całego interwału. Raport jakości wykrywa duplikaty, niemonotoniczne timestampy, luki, błędne rekordy, niekompletne odpowiedzi, nieaktualne dane i mieszane źródła. Brakujących świec nie interpoluje. Analiza jest blokowana, gdy raport jakości jest nieakceptowalny; niedomknięta ostatnia świeca nie potwierdza sygnału. RSI14 i ATR14 używają wygładzania Wildera, a bezpośrednie obliczenie wskaźników odrzuca niekompletne, niepoprawne lub nieuporządkowane świece.

CCXT REST ma włączony natywny rate limiter giełdy, timeout i ponawianie błędów sieciowych/limitów (w tym odpowiedzi klasy 429), a adapter ogranicza współbieżność i używa krótkiego cache. Historia pobierana jest stronami `since` z deduplikacją; limit stron jest kontrolowany, a częściowe pobranie jest oznaczane i blokuje sygnały do czasu dalszej synchronizacji. Tryb historii zapisuje parametry pobrania, zakres, status jakości, giełdę/źródło i kompletność w SQLite. Lokalne cache świec umożliwia kontynuację synchronizacji po restarcie.

Tickeri i publiczny order book są pobierane przez REST, gdy wybrana giełda wspiera endpointy. Pola niedostępne u dostawcy pozostają `null`/brakujące. Spread pochodzi z bid/ask, jeśli dostępne; w przeciwnym wypadku analiza jawnie używa zakresu świecy jako przybliżenia. Poziomy order booka pozwalają oszacować impact dla `TRADINGAGENT_COST_ESTIMATE_NOTIONAL`; jeśli książka jest za płytka albo nieobsługiwana, program wraca do jawnie skonfigurowanego poślizgu. To snapshot, nie historyczna płynność ani gwarancja realizacji po tej cenie.

Serwer API używa publicznego strumienia transakcji CCXT Pro (`watchTradesForSymbols`), jeśli dana giełda go wspiera. Transakcje zwrócone przez feed są agregowane inkrementalnie do świec; niepełne świece są stanem na żywo i nie trafiają do analizy ani bazy jako zamknięte, dopóki nie upłynie ich interwał. Po starcie lub reconnectie pomijana jest świeca rozpoczęta wcześniej, a przerwy w strumieniu nie są wypełniane sztucznymi świecami. To przetwarza każde zdarzenie dostarczone przez wybrany feed, ale nie gwarantuje obserwacji każdego zdarzenia giełdy. Jeżeli strumień jest niedostępny, serwer korzysta z REST ticker snapshots, domyślnie co 5 sekund (`TRADINGAGENT_MARKET_REST_POLL_SECONDS`, zakres 1–300); rzeczywisty czas między odświeżeniami symbolu może być dłuższy o czas zapytań i ograniczenia rate limitu. Migawki nie zawierają wszystkich transakcji i nie dają świec tickowych; zamknięte OHLCV jest uzupełniane przez synchronizację REST, domyślnie co 60 sekund. Zdrowie, źródło, opóźnienie, ponowienia oraz liczniki zdarzeń są dostępne w `GET /api/market-data/health` i Diagnostyce dashboardu.

SQLite jest domyślnym magazynem lokalnym, odpowiednim dla pojedynczej instancji i umiarkowanego wolumenu. Świece mają klucz z giełdą/symbolem/interwałem/czasem oraz źródło danych (np. `ccxt`) i flagę kompletności. Giełda (np. `binance`) i dostawca danych są osobnymi informacjami; analiza dopuszcza wyłącznie jeden, rozpoznany dostawca w obrębie historii. Raport backtestu zachowuje pochodzenie źródła. Schemat dla starszej bazy uzupełniany jest przy otwarciu. Zmiana na PostgreSQL powinna zachować te klucze i metadane; interfejs repozytorium można odseparować przed skalowaniem do wielu procesów.

API, dashboard i synchronizację uruchamia dodatek `[api]`, Telegram jest opcjonalny i wyłączony domyślnie, a LLM jest opcjonalny i nie jest potrzebny do analizy.

## Konfiguracja przed pierwszym uruchomieniem

Po `cp .env.example .env` otwórz `.env` i ustaw co najmniej giełdę, kwotowaną parę oraz interwały obsługiwane przez giełdę. Przykładowe ustawienia domyślne są przygotowane dla `binance`, `BTC/USDT,ETH/USDT,XRP/USDT,SOL/USDT` i `5m,15m,1h,4h,1d`; to wartości przykładowe, a dostępność każdej pary/interwału trzeba potwierdzić na wybranej giełdzie. Nie wpisuj kluczy giełdowych — aplikacja nie używa prywatnych endpointów konta.

| Zmienna | Wymagana? | Znaczenie |
|---|---:|---|
| `TRADINGAGENT_EXCHANGE` | Tak, jeśli zmieniasz giełdę | Identyfikator CCXT, np. `binance`, małymi literami. |
| `TRADINGAGENT_SYMBOLS` | Opcjonalna | Lista rozdzielona przecinkami, np. `BTC/USDT,ETH/USDT`. Musi odpowiadać rynkom giełdy. |
| `TRADINGAGENT_TIMEFRAMES` | Opcjonalna | Interwały pobierane przez synchronizację, np. `5m,15m,1h,4h,1d`. |
| `TRADINGAGENT_DB_PATH` | Opcjonalna | SQLite; domyślnie `data/market.sqlite3`. Katalog tworzony automatycznie. |
| `TRADINGAGENT_MODE` | Opcjonalna | `analysis` (domyślnie) lub `paper`; `live` jest odrzucany. |
| `TRADINGAGENT_ENABLED_STRATEGIES` | Opcjonalna | Aktywne reguły rozdzielone przecinkami. Szczegóły i progi w `.env.example`. |
| `TRADINGAGENT_STRATEGY_ENTRY_TIMEFRAME`, `TRADINGAGENT_STRATEGY_HIGHER_TIMEFRAME` | Opcjonalne | Interwały strategii; oba muszą być objęte pobieraniem i dostępne na giełdzie. |
| `TRADINGAGENT_TIMEOUT_MS`, `TRADINGAGENT_LIMIT`, `TRADINGAGENT_MAX_CONCURRENCY` | Opcjonalne | Limity sieci, rozmiar pobrania i współbieżność. |
| `TRADINGAGENT_API_TOKEN` | Wymagany dla API poza localhost; zalecany dla API | Losowy sekret Bearer. Zostaw pusty tylko dla lokalnego użycia. |
| `TRADINGAGENT_API_SYNC_ENABLED`, `TRADINGAGENT_API_SYNC_SECONDS` | Opcjonalne | Włączenie pobierania przez serwer i jego odstęp. Domyślnie włączone, 60 sekund. |
| `TRADINGAGENT_MARKET_REST_POLL_SECONDS` | Opcjonalne | Publiczne migawki tickerów REST jako fallback, domyślnie 5 s, zakres 1–300 s; nie są to wszystkie transakcje. |
| `TRADINGAGENT_PAPER_AUTO_ENABLED`, `TRADINGAGENT_PAPER_AMOUNT_STEPS` | Wymagane tylko dla automatycznego paper | Worker wymaga jawnego `true`, trybu `paper` i osobnego kroku ilości dla każdego skonfigurowanego symbolu. Wartości przepisz z precyzji rynku w metadanych giełdy; dla jednego symbolu nadal obsługiwane jest starsze `TRADINGAGENT_PAPER_AMOUNT_STEP`. Domyślnie worker wyłączony. |
| `TRADINGAGENT_TELEGRAM_ENABLED`, `TRADINGAGENT_TELEGRAM_BOT_TOKEN`, `TRADINGAGENT_TELEGRAM_CHAT_ID` | Wymagane tylko dla Telegrama | Bot token i chat ID; trzymaj wyłącznie w lokalnym `.env`/magazynie sekretów. |
| `TRADINGAGENT_LLM_ENABLED`, `TRADINGAGENT_LLM_ENDPOINT`, `TRADINGAGENT_LLM_MODEL`, `TRADINGAGENT_LLM_API_KEY` | Wymagane tylko dla LLM | Endpoint HTTPS zgodny z Chat Completions, nazwa modelu i klucz dostawcy. LLM domyślnie wyłączony. |

Limity w `.env.example` są przykładami demonstracyjnymi, a nie poradą ani gwarancją bezpieczeństwa. Koszty prowizji, spreadu i slippage dopasuj do źródła i symulacji. Jeżeli dostawca nie podaje bid/ask lub głębokości, aplikacja nie udaje, że zna te wartości.

### Typowe uruchomienia

```bash
# Walidacja ustawień i diagnostyka publicznego API giełdy
tradingagent --check-config
tradingagent --check-connection

# Jednorazowa synchronizacja/analiza wszystkich skonfigurowanych aktywów i interwałów
tradingagent

# Analiza wyłącznie z zapisanej bazy, bez sieci
tradingagent --offline

# Serwer API, dashboard i okresowa synchronizacja
tradingagent --serve

# Serwer dostępny z innej maszyny — ustaw wcześniej silny TRADINGAGENT_API_TOKEN
tradingagent --serve --host 0.0.0.0 --port 8000
```

Samo utworzenie `.env` nie pobiera danych. Użyj `tradingagent`, `--history` albo serwera z synchronizacją włączoną. Pierwsze uruchomienie może nie wygenerować sygnału, dopóki nie zostanie zebrana historia rozgrzewkowa (co najmniej 50 świec dla analizy). Brak połączenia oznacza brak świeżych danych i brak nowych sygnałów.

Aby sprawdzić konfigurację i publiczny dostęp, pobrać historię, pokazać ostatnie świece lub zweryfikować zapisane dane:

```bash
tradingagent --check-config
tradingagent --check-connection
tradingagent --history BTC/USDT --timeframe 5m --since 2025-01-01T00:00:00Z --until 2025-01-02T00:00:00Z
tradingagent --last-candles BTC/USDT --timeframe 15m --count 20
tradingagent --quality BTC/USDT --timeframe 1h
```

Aby analizować zapisane świece bez połączenia z giełdą: `tradingagent --offline`. JSON: `tradingagent --json`.

## Reguły pierwszego etapu

- Reżim wzrostowy/spadkowy wymaga zgodnego położenia ceny i co najmniej 0,3% różnicy EMA12/EMA26; pozostałe przypadki to ruch boczny.
- Scenariusz long/short wymaga zgodnego reżimu i zakresu RSI14 50–70 / 30–50.
- Strefa wejścia to ±0,2 ATR od ostatniego zamknięcia, stop i unieważnienie 1,5 ATR, cele 2R i 3R.
- Koszt obejmuje prowizje, spread i poślizg z książki zleceń dla skonfigurowanej wielkości albo skonfigurowane założenie, gdy depth jest niedostępny. Szacunkowy RR odejmuje ten koszt.
- Jakość sygnału jest deterministycznym scoringiem warunków, a nie prawdopodobieństwem zysku.
- Przy braku co najmniej 50 świec analiza nie podaje scenariusza.

## Strategy Engine

Wyniki `strategy_signals` są oddzielone od ogólnej analizy rynku. Silnik uruchamia niezależnie konfigurowalne reguły `trend_following`, `pullback`, `breakout_retest`, `momentum` oraz opcjonalne `mean_reversion`. Używa tylko zamkniętych świec i wyników Market Analysis na interwale wejścia oraz wyższym. `mean_reversion` domyślnie jest wyłączone i wymaga bocznego reżimu na obu interwałach. Pullback wymaga potwierdzenia świecą powrotu; samo dotknięcie EMA nie wystarcza. Breakout wymaga wcześniejszego zamknięcia z potwierdzeniem wolumenu oraz retestu poziomu.

Przełączniki, progi i wagi w kolejności trend/struktura/momentum/wolumen/interwały/RR/dane/warunki strategii znajdują się w `.env.example`: `TRADINGAGENT_ENABLED_STRATEGIES`, `TRADINGAGENT_STRATEGY_ENTRY_TIMEFRAME`, `TRADINGAGENT_STRATEGY_HIGHER_TIMEFRAME`, `TRADINGAGENT_STRATEGY_MIN_NET_RR`, `TRADINGAGENT_STRATEGY_MIN_SCORE` oraz `TRADINGAGENT_SCORE_WEIGHTS`. Każda reguła zwraca również `NO TRADE`, gdy nie zachodzą warunki. Sygnał zawiera wynik ważony z osobnymi składowymi (nie jest to prawdopodobieństwo zysku), poziomy obliczone z danych, koszty, wskaźniki, wyjaśnienie i czynniki przeciwne. Spread niedostępny pozostaje jawnie nieznany. Silnik nie wysyła zleceń.

## Risk Management

`tradingagent.risk.RiskManager` jest odrębną bramką, która przyjmuje sygnał, stan portfela, parametry instrumentu i świeżość/jakość danych. Wielkość wylicza z budżetu straty podzielonego przez odległość wejście–stop powiększoną o koszty; zaokrągla ją w dół do kroku ilości i ogranicza przez ekspozycję, limity aktywa/grupy oraz dostępny limit płynności. Nie koryguje poziomów strategii. Wynik zawiera przewidywaną stratę przy stopie oraz stresową stratę po niekorzystnym poślizgu. `paper_trading.authorize_paper_entry` odrzuca propozycję, jeśli bramka ryzyka jej nie zatwierdzi. Egzekucja giełdowa pozostaje wyłączona.

Limity środowiskowe są w `.env.example`. Wartości są wyłącznie demonstracyjne i nie gwarantują bezpieczeństwa ani zysku. Brak lub awaria danych, przekroczenie limitu portfela i krytyczne błędy aktywują zatrzaskowy circuit breaker; jego reset wymaga jawnego wywołania `reset()`. Implementacja zakłada pozycje LONG i kwotowanie w walucie instrumentu; precyzję ilości oraz limit płynności musi przekazać adapter instrumentu, nie są zgadywane z symbolu.

## Paper Trading i Backtesting

Trwały paper account jest dostępny przez `PaperTradingEngine(db_path, account_id, initial_capital, quote_currency, fee_bps, slippage_bps, spread_bps)`. Najpierw zapisuje sygnał (`record_signal`), a osobno wykonuje wejście (`execute_entry`) po zatwierdzeniu Risk Managera. Wypełnienie jest dozwolone wyłącznie na otwarciu kolejnej świecy, jeśli cena mieści się w strefie sygnału. Pozycje, fills, częściowe zamknięcia, prowizje, historię zdarzeń i equity curve zapisuje w SQLite. Przy świecy obejmującej jednocześnie stop i cel przyjęto konserwatywnie stop jako pierwszy; luka przez stop wykonuje się po gorszym otwarciu. `run_paper_cycle` łączy walidację, strategię, scoring, niezależną bramkę ryzyka i paper fill. Serwer uruchamia go automatycznie wyłącznie po jawnym opt-in z tabeli konfiguracji powyżej. Zalecana pierwsza konfiguracja to tryb `analysis`, sprawdzenie sygnałów i portfela, a dopiero potem ewentualne włączenie symulacji. Nawet w trybie `paper` nie są używane środki rzeczywiste ani prywatne endpointy giełdy.

Backtest na zapisanej historii: `tradingagent --backtest BTC/USDT --since 2025-01-01T00:00:00Z --until 2025-04-01T00:00:00Z --entry-timeframe 15m --higher-timeframe 4h --amount-step 0.00001 --report-dir reports`. Zakres końcowy jest wyłączny; używane są tylko świece, które zamknęły się przed jego końcem. Sygnał powstaje po zamknięciu świecy, a wejście jest symulowane na otwarciu następnej. Niekompletne świece są pomijane, luki nie są uzupełniane, oczekujący sygnał jest anulowany, a otwarta pozycja zamykana przy następnym obserwowanym otwarciu i rozpoczyna się nowa rozgrzewka wskaźników. Wynik zawiera hash danych, fills, transakcje, koszty, ROI, drawdown, win rate, profit factor, expectancy, niepewność win rate, reżimy i benchmark gotówki/buy-and-hold. Raporty JSON/CSV oraz wykresy SVG są zapisywane, jeśli podano `--report-dir`.

`--compare-strategies` uruchamia wybrane strategie niezależnie na tych samych danych, kosztach i zakresie. `chronological_split` daje rozłączne okresy design/validation/final-test; aplikacja nie stroi parametrów na końcowym okresie. `run_universe` raportuje osobno wspierane instrumenty z równym przydziałem kapitału do niezależnych rachunków, nie jest to wspólny portfel z korelacją między symbolami.

## Testy i ograniczenia

`python -m pytest` uruchamia testy logiki, fixture’ów offline, symulacji paper/backtestera, dostawcy, API i SQLite. Historyczny slippage jest założeniem opartym na konfiguracji, ponieważ OHLCV nie zawiera historycznego order booka; backtest ogranicza wielkość przez udział w wolumenie świecy. To nie gwarantuje jakości realizacji ani przyszłego wyniku.

## Opcjonalne ML i LLM

Podstawowa analiza regułowa działa niezależnie. `tradingagent.ai.HistoricalEventModel` trenuje deterministyczny model logistyczny wyłącznie na przekazanych, kompletnych świecach BTC/ETH/XRP/SOL. Zdarzenie oznacza osiągnięcie wzrostu `target_return` po kosztach przed spadkiem `max_adverse_drawdown` w `horizon` świec; gdy stop i cel są w obrębie tej samej świecy, etykieta jest negatywna. Cechy korzystają tylko z danych do chwili prognozy. Trening, walidacja i końcowy test są chronologiczne, z przerwą co najmniej równą horyzontowi etykiety. Raport zawiera baseline częstości zdarzenia, Brier score, trafność, kalibrację, wersję cech/modelu i hash zbioru. Minimalna praktyczna historia to co najmniej 100 świec, a podziały wymagają dodatkowych przykładów. Pipeline nie pobiera ani nie syntetyzuje danych. Trening uruchomisz z Python API, np.:

```python
from tradingagent.ai import HistoricalEventModel, MLConfig
from tradingagent.config import Settings
from tradingagent.storage import CandleStore

settings = Settings.from_env()
history = CandleStore(settings.db_path).load(settings.exchange, "BTC/USDT", "1h", limit=None)
report = HistoricalEventModel(MLConfig()).train(history, "BTC/USDT", "data/models/btc-1h.json")
print(report.metrics)
```

Najpierw pobierz wystarczającą prawdziwą historię poleceniem `tradingagent --history`; końcowe okno testowe i koszty konfiguruj świadomie. Wynik historyczny nie jest gwarancją ani poradą transakcyjną.

`LLMAnalyst` jest opcjonalnym klientem HTTPS zgodnym z formatem Chat Completions. Jest wyłączony domyślnie; ustawienia endpointu, modelu, klucza, timeoutu, limitów wejścia/wyjścia oraz kosztu są w `.env.example`. Klucz trafia wyłącznie do nagłówka HTTP, nigdy do danych promptu. Koszt jest szacowany konserwatywnie z maksymalnego limitu odpowiedzi i stawek wpisanych przez operatora; zerowe stawki wyłączają szacunek cenowy, dlatego przed włączeniem należy sprawdzić ceny dostawcy. `explain_safely` zwraca brak interpretacji przy błędzie, nie blokując silnika reguł. Odpowiedź podlega walidacji schematu. `evidence_report` rozdziela fakty, obliczenia, subiektywną interpretację i niepewność.

`MarketContext` przechowuje timestamp, pochodzenie, jakość i status weryfikacji dla wiadomości, wydarzeń makro, nastroju oraz danych pochodnych. `context_as_of` odrzuca informacje z przyszłości dla raportów historycznych. To kontrakt/adapter normalizacyjny dla dostarczonych rekordów, a nie aktywny konektor do dostawców newsów, kalendarzy, sentiment, open interest czy funding. Nie skonfigurowano takich źródeł ani ich kluczy, więc brak tych danych pozostaje jawny; nagłówki są niezaufanym tekstem i same nie tworzą sygnału.

## Dashboard, API, Telegram i monitoring

### Fedora i uruchomienie

Python 3.11+:

```bash
sudo dnf install python3 python3-pip
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev,api]'
cp .env.example .env
```

Skonfiguruj `.env`, następnie pobierz historię poleceniem `tradingagent --history BTC/USDT --timeframe 15m --since 2025-01-01T00:00:00Z --until 2025-01-02T00:00:00Z`. Uruchom serwer poleceniem `tradingagent --serve` i otwórz `http://127.0.0.1:8000`. Dokumentacja API jest pod `/docs`. Serwer domyślnie synchronizuje publiczne dane CCXT co minutę; interwał ustawia `TRADINGAGENT_API_SYNC_SECONDS`, a `TRADINGAGENT_API_SYNC_ENABLED=false` wyłącza pobieranie. Nie są potrzebne prywatne klucze giełdy. Panel pobiera tylko zapisane dane; braki pokazuje jawnie.

Endpointy odczytowe: `/health`, `/api/status`, `/api/market`, `/api/assets/{symbol}`, `/api/candles/{symbol}?timeframe=15m&limit=200`, `/api/signals`, `/api/signals/history`, `/api/strategies`, `/api/settings`, `/api/backtests` oraz `/api/paper`. Endpointy list mają limity i paginację. Ustaw losowy `TRADINGAGENT_API_TOKEN`, aby wymagać nagłówka `Authorization: Bearer ...`; przy adresie `--host` innym niż localhost token jest obowiązkowy. API nie przyjmuje zleceń giełdowych. `/api/market` pokazuje ostatni świeży zapisany ticker, jeśli istnieje; w przeciwnym razie używa ostatniej zamkniętej świecy i oznacza jej nieaktualność. To nie są gwarantowane notowania tick-by-tick.

### Telegram

Ustaw `TRADINGAGENT_TELEGRAM_ENABLED=true`, token bota i chat ID. CLI wysyła sygnały oraz utrwala ich fingerprinty w SQLite, ograniczając duplikaty; błąd powiadomienia nie wpływa na analizę. `TelegramNotifier.notify(category, key, message)` udostępnia bazowy kanał do innych zdarzeń. Powiadomienia o wyjściu paper, poziomach ryzyka, zmianie reżimu i podsumowaniach okresowych nie mają jeszcze zintegrowanego producenta zdarzeń.

### Monitoring, backup i ograniczenia

Logi JSON włącza `tradingagent.monitoring.configure_logging`; `ServiceHealth` przechowuje ostatni sukces, liczbę błędów i status komponentu. `/health` sprawdza SQLite i synchronizację, `/api/status` pokazuje świeżość danych. Nie ma zewnętrznego systemu metryk ani alarmowania.

Kopia SQLite po zatrzymaniu aplikacji: `sqlite3 data/market.sqlite3 ".backup 'data/market-backup.sqlite3'"`. Sprawdź kopię przez `sqlite3 data/market-backup.sqlite3 'PRAGMA integrity_check;'`. Archiwizuj także `reports/` i `data/models/`. `.env` przechowuj wyłącznie w zaszyfrowanym magazynie sekretów.

Dashboard pokazuje świeczki i wskaźniki/poziomy jako osobne wartości, nie nakłada EMA na wykres. Automatyczne paper wykonanie jest domyślnie wyłączone. Można je włączyć tylko w `TRADINGAGENT_MODE=paper` przez `TRADINGAGENT_PAPER_AUTO_ENABLED=true` oraz kompletne `TRADINGAGENT_PAPER_AMOUNT_STEPS` w formacie `BTC/USDT=<krok>,ETH/USDT=<krok>,XRP/USDT=<krok>,SOL/USDT=<krok>`; każdą wartość trzeba sprawdzić w metadanych konkretnej giełdy i rynku. Dla jednego symbolu dopuszczalne jest starsze `TRADINGAGENT_PAPER_AMOUNT_STEP`. Worker używa wyłącznie zamkniętych świec do sygnału, następnego otwarcia jako ceny wykonania, Risk Managera, danych źródłowych i circuit breakera; nie łączy się z prywatnymi endpointami ani nie wysyła zleceń. Przy braku precyzji dla któregokolwiek symbolu pozostaje wyłączony. Paper nie symuluje zaokrąglania ceny do ticka giełdy; tick price jest potrzebny dopiero przy integracji zleceń, której nie ma. Backtesty trzeba uruchomić CLI; API pokazuje zapisane wyniki. Tryb `live` pozostaje zablokowany, a żaden endpoint nie wysyła rzeczywistych zleceń. Nie ma aktywnych konektorów newsów, kalendarza ekonomicznego i nastrojów.

### Testy i typowe problemy

```bash
python -m pytest
```

Jeśli pojawi się `No module named pytest`, aktywuj `.venv` i zainstaluj `python -m pip install -e '.[dev,api]'`. Jeśli `tradingagent --serve` zgłasza brak FastAPI/Uvicorn, doinstaluj `python -m pip install -e '.[api]'`. Jeśli polecenie `tradingagent` nie istnieje, sprawdź aktywację virtualenv i wykonaj ponownie `python -m pip install -e '.[dev,api]'`. Błędy połączenia, HTTP 429, brak pary/interwału lub ograniczenia geograficzne oznaczają problem dostępności wybranej giełdy; sprawdź `tradingagent --check-connection` i symbole obsługiwane na tej giełdzie. Pusty dashboard przed pierwszą synchronizacją jest oczekiwany — uruchom CLI lub poczekaj na przebieg synchronizacji i sprawdź `/api/status`. Nie obniżaj limitu wieku danych, aby ukryć brak aktualizacji.

`python -m pip install -e .` instaluje pakiet runtime CCXT. `[dev]` dodaje pytest, `[api]` dodaje FastAPI/Uvicorn. Nie ma wymaganej usługi Node, zewnętrznego serwera SQL ani osobnych bibliotek wykresów. Działanie zewnętrznego API giełdy wymaga Internetu; odczyt lokalnej bazy, backtest zapisanych danych i testy jednostkowe nie powinny wymagać dostępu do giełdy.

## Web terminal — uruchamianie i sterowanie

W repozytorium znajduje się lokalny dashboard serwowany przez istniejące FastAPI. Na Fedorze uruchom jednorazowo instalację zależności w środowisku projektu, a potem startuj serwer skryptem:

```bash
cd ~/tradingagent
python3 -m venv .venv                 # pomiń, jeśli .venv już istnieje
source .venv/bin/activate
python -m pip install -e '.[api]'
./run_web.sh
```

Skrypt wybiera `.venv` projektu, sprawdza Python i zależności, domyślnie wiąże serwer wyłącznie z `127.0.0.1:8000`, odmawia startu przy zajętym porcie lub drugiej kopii z tego checkoutu oraz zapisuje log serwera do `data/web.log`. Otwórz **http://127.0.0.1:8000**. Zatrzymanie: `Ctrl+C`. Inny lokalny port można wskazać przez `TRADINGAGENT_WEB_PORT=8001 ./run_web.sh`. Skrypt nie instaluje pakietów automatycznie.

Dashboard odpyta API co 20 sekund; nie wymaga osobnego serwera frontendowego. Ręczne przyciski uruchamiają backendowe operacje: synchronizację danych, analizę i zapis historii sygnałów, start/stop paper trading, zamknięcie symulowanej pozycji, reset konta po dodatkowym potwierdzeniu oraz asynchroniczny backtest na danych zapisanych w SQLite. Formularz backtestu nie pobiera historii — najpierw trzeba ją zsynchronizować. Wynik backtestu jest pokazywany dopiero po poprawnym zakończeniu zadania.

Ustawienia edytowalne w panelu są walidowane i trwale zapisywane w tabeli `web_settings` tej samej bazy. Źródłem danych pozostaje CCXT, a do paper/backtestu trzeba wpisać krok ilości z precyzji konkretnego rynku. Synchronizacja oraz pobieranie świeżych cen wymagają dostępu do publicznego API giełdy; przy braku sieci dashboard nadal pokazuje backend, zapisane dane, ich wiek oraz błąd źródła. Nie wyświetla fikcyjnych cen.

API sterujące jest chronione przez `TRADINGAGENT_API_TOKEN`, jeśli ta zmienna jest ustawiona; dashboard poprosi o token i przechowuje go tylko w pamięci bieżącej karty. Skrypt startowy nie wystawia serwera poza localhost. Nie dodano endpointów prawdziwych zleceń. Strategia ocenia LONG i SHORT niezależnie, a paper trading może symulować oba kierunki; symulowany SHORT księguje wpływ ze sprzedaży i zobowiązanie do odkupienia, bez rzeczywistego pożyczania aktywów lub dźwigni, więc nie jest zwykłym handlem spot. Parametry okresów wskaźników pozostają zdefiniowane w kodzie, a Telegram/LLM nie są konfigurowane z przeglądarki.

Nowe endpointy sterujące: `POST /api/control/analysis`, `POST /api/control/refresh`, `POST /api/control/paper`, `PUT /api/settings`, `POST /api/backtests/run`, `GET /api/backtests/status`, `GET /api/backtests/{id}` i `GET /api/diagnostics`. Dokumentacja OpenAPI jest dostępna pod `/docs`.
