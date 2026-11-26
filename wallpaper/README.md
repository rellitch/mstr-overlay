# Live Ticker Wallpaper (Windows)

A live desktop background: one big TradingView chart plus a scrolling ticker tape.
Default: Bitcoin chart; tape shows BTC, SPY, MSTR, ASST.

## Set up (about 2 minutes)

1. Install **Lively Wallpaper** (free): Microsoft Store, or https://github.com/rocksdanister/lively/releases
2. Get `LiveTickerWallpaper.zip`: the files in this folder, zipped with `LivelyInfo.json` at the top level of the zip.
3. Open Lively, click **+ (Add Wallpaper)**, choose the zip (or drag it onto the Lively window).
4. Click the new "Live Ticker Wallpaper" tile to set it as your background.

## Change the tickers

Right-click the wallpaper's tile in Lively, choose **Customise**, and edit:

| Setting | What it does |
|---|---|
| Ticker tape | Comma-separated list. Plain tickers (`AAPL`) or `EXCHANGE:SYMBOL` (`BINANCE:ETHUSDT`). `BTC`, `ETH`, `SOL` map to Coinbase USD pairs. |
| Big chart symbol | The symbol shown in the large chart. |
| Chart timeframe | 1 minute to 1 week. |
| Theme | Dark or light. |
| Show chart toolbars | Off keeps the chart clean. |
| Space at bottom / left | Keeps the chart clear of the taskbar and desktop icons. |

## Notes

- Data comes from TradingView's free widgets. Crypto is real-time; some stock quotes may be delayed.
- Clicks on the ticker tape are blocked so clicking the desktop doesn't open a browser. The chart itself stays interactive (hover, scroll to zoom).
- Widgets reload every 6 hours and when the network reconnects, to recover after sleep.
- To preview in a browser, open `index.html`; settings can be passed in the URL, e.g.
  `index.html?tickers=BTC,SPY,MSTR,ASST&chart=BTC&interval=60&theme=dark`
