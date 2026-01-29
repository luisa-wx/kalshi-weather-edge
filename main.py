"""
WX Sniper - Main Entry Point

Run modes:
1. webhook - Start Flask server to receive SMS webhooks + auto-poll aviationweather
2. poll - Only poll aviationweather.gov (no SMS)
3. test - Run tests with sample data
4. discover - Discover Kalshi ticker formats
5. ui - Run web UI for manual METAR entry
"""

import sys
import time
import argparse
import os
from datetime import datetime, timezone
from threading import Thread

from config import (
    STATIONS,
    SYNOPTIC_HOURS_UTC,
    SMS_POLL_START_SECONDS_BEFORE,
    SMS_POLL_INTERVAL_SECONDS,
    SMS_POLL_MAX_DURATION_SECONDS,
    TWILIO_ACCOUNT_SID
)
from metar_parser import parse_metar, format_metar_summary
from kalshi_client import KalshiClient
from twilio_handler import TwilioSMSClient, create_webhook_app, set_metar_callback
from aviation_weather import AviationWeatherPoller
from trader import WXSniper, metar_callback, get_sniper
from state_manager import StateManager


def create_app():
    """
    Factory function for gunicorn/production deployment.
    Returns a Flask app with background polling thread started.
    """
    from flask import Flask, request, render_template_string, jsonify
    from config import (
        POLL_INTERVAL_NORMAL_SECONDS,
        POLL_INTERVAL_HOT_SECONDS,
        HOT_WINDOW_START_MINUTE,
        HOT_WINDOW_END_MINUTE
    )
    
    # Check if we're in live mode via environment variable
    dry_run = os.environ.get("LIVE_TRADING", "false").lower() != "true"
    
    print("=" * 60)
    print("WX SNIPER - STARTING")
    print("=" * 60)
    print(f"Dry run: {dry_run}")
    print(f"Set LIVE_TRADING=true env var to enable real trades")
    print("=" * 60)
    
    # Initialize components
    sniper = get_sniper(dry_run=dry_run)
    poller = AviationWeatherPoller()
    
    # Set up SMS callback
    set_metar_callback(metar_callback)
    
    # Create Flask app
    app = create_webhook_app()
    
    # Manual METAR entry page
    MANUAL_UI_HTML = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>WX Sniper</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; background: #1a1a2e; color: #eee; }
            h1 { color: #00ff88; }
            textarea { width: 100%; height: 120px; font-family: monospace; font-size: 14px; background: #16213e; color: #eee; border: 1px solid #0f3460; padding: 10px; }
            button { background: #00ff88; color: #1a1a2e; border: none; padding: 15px 30px; font-size: 18px; cursor: pointer; margin-top: 10px; }
            button:hover { background: #00cc6a; }
            .result { margin-top: 20px; padding: 15px; background: #16213e; border-radius: 5px; }
            .success { border-left: 4px solid #00ff88; }
            .error { border-left: 4px solid #ff4444; }
            .status { margin-top: 20px; padding: 10px; background: #0f3460; border-radius: 5px; font-size: 12px; }
            pre { white-space: pre-wrap; word-wrap: break-word; }
        </style>
    </head>
    <body>
        <h1>🌡️ WX Sniper</h1>
        <p>Paste METAR from Leidos SMS below:</p>
        
        <textarea id="metar" placeholder="KSFO 281853Z 29012KT 10SM FEW020 SCT200 17/08 A3012 RMK AO2 SLP203 T01720083 10189 20156 58010"></textarea>
        <br>
        <button onclick="submitMetar()">🎯 Process METAR</button>
        
        <div id="result"></div>
        
        <div class="status">
            <strong>Status:</strong> <span id="status">Ready</span><br>
            <strong>Last Update:</strong> <span id="lastUpdate">-</span><br>
            <strong>Mode:</strong> ''' + ("DRY RUN 🧪" if dry_run else "🔴 LIVE TRADING") + '''<br>
            <strong>Polling:</strong> Normal=60s, Hot window (:51-:58)=5s
        </div>
        
        <script>
            async function submitMetar() {
                const metar = document.getElementById('metar').value;
                document.getElementById('status').textContent = 'Processing...';
                
                try {
                    const response = await fetch('/api/process', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({metar: metar})
                    });
                    const data = await response.json();
                    
                    let html = '<div class="result ' + (data.success ? 'success' : 'error') + '">';
                    html += '<strong>' + (data.success ? '✅ Processed' : '❌ Error') + '</strong><br>';
                    html += '<pre>' + JSON.stringify(data, null, 2) + '</pre>';
                    html += '</div>';
                    
                    document.getElementById('result').innerHTML = html;
                    document.getElementById('status').textContent = 'Ready';
                    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
                } catch (e) {
                    document.getElementById('result').innerHTML = '<div class="result error">Error: ' + e + '</div>';
                    document.getElementById('status').textContent = 'Error';
                }
            }
        </script>
    </body>
    </html>
    '''
    
    @app.route('/')
    def index():
        return MANUAL_UI_HTML
    
    @app.route('/api/process', methods=['POST'])
    def api_process():
        """API endpoint for manual METAR submission"""
        data = request.get_json()
        metar_text = data.get('metar', '').strip()
        
        if not metar_text:
            return jsonify({'success': False, 'error': 'No METAR provided'})
        
        try:
            results = sniper.process_metar(metar_text)
            
            return jsonify({
                'success': True,
                'metar': metar_text[:60] + '...',
                'trades': [
                    {
                        'station': r.signal.station if r.signal else 'N/A',
                        'type': r.signal.bracket_type if r.signal else 'N/A',
                        'temp': r.signal.temperature_f if r.signal else 'N/A',
                        'market': r.market_ticker,
                        'executed': r.success,
                        'error': r.error
                    }
                    for r in results
                ]
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    
    @app.route('/api/status')
    def api_status():
        """Get current bot status"""
        state = sniper.state
        return jsonify({
            'time': datetime.now(timezone.utc).isoformat(),
            'dry_run': dry_run,
            'stations': {
                station: {
                    'high': state.get_state(station).tracked_high_f,
                    'low': state.get_state(station).tracked_low_f
                }
                for station in STATIONS.keys()
            }
        })
    
    # Background polling thread
    def poll_aviation_weather():
        """Poll aviationweather.gov with adaptive rate"""
        print(f"[POLL] Starting adaptive polling:")
        print(f"       Normal: every {POLL_INTERVAL_NORMAL_SECONDS}s")
        print(f"       Hot window (:51-:58 on synoptic hours): every {POLL_INTERVAL_HOT_SECONDS}s")
        
        last_log_minute = -1
        
        while True:
            try:
                now = datetime.now(timezone.utc)
                hour = now.hour
                minute = now.minute
                
                # Check if we're in a hot window
                is_synoptic_hour = hour in SYNOPTIC_HOURS_UTC
                is_hot_window = is_synoptic_hour and HOT_WINDOW_START_MINUTE <= minute <= HOT_WINDOW_END_MINUTE
                
                if is_hot_window:
                    interval = POLL_INTERVAL_HOT_SECONDS
                    window_label = "🔥 HOT"
                else:
                    interval = POLL_INTERVAL_NORMAL_SECONDS
                    window_label = "💤 normal"
                
                # Fetch new METARs
                new_metars = poller.check_for_new_metars()
                
                for metar in new_metars:
                    if poller.is_synoptic_metar(metar):
                        print(f"[POLL] 📊 SYNOPTIC METAR detected: {metar.station}")
                        results = sniper.process_metar(metar.raw_text)
                        
                        for r in results:
                            if r.success:
                                print(f"[POLL] ✅ Trade executed: {r.market_ticker}")
                            else:
                                print(f"[POLL] ⏭️ No trade: {r.error}")
                    elif is_hot_window:
                        print(f"[POLL] Regular METAR (no 6hr groups): {metar.station}")
                
                # Log status during hot window
                if is_hot_window and minute != last_log_minute:
                    print(f"[POLL] {window_label} | {now.strftime('%H:%M:%SZ')} | polling every {interval}s")
                    last_log_minute = minute
                        
            except Exception as e:
                print(f"[POLL] Error: {e}")
            
            time.sleep(interval)
    
    # Start polling thread
    poll_thread = Thread(target=poll_aviation_weather, daemon=True)
    poll_thread.start()
    
    print("\n✅ WX Sniper ready!")
    print(f"   - Web UI: /")
    print(f"   - SMS Webhook: /sms/webhook")
    print(f"   - Health: /health")
    print(f"   - Adaptive polling active\n")
    
    return app


def run_hybrid_server(host: str = "0.0.0.0", port: int = 5000, dry_run: bool = True):
    """
    Run hybrid mode: 
    - Flask webhook for receiving forwarded SMS
    - Background polling of aviationweather.gov
    - Web UI for manual METAR paste
    
    This is the main production mode.
    """
    print("=" * 60)
    print("WX SNIPER - HYBRID MODE")
    print("=" * 60)
    print(f"Dry run: {dry_run}")
    print(f"Webhook: http://{host}:{port}/sms/webhook")
    print(f"Manual UI: http://{host}:{port}/")
    print("=" * 60)
    
    # Initialize components
    sniper = get_sniper(dry_run=dry_run)
    poller = AviationWeatherPoller()
    
    # Set up SMS callback
    set_metar_callback(metar_callback)
    
    # Create Flask app with added manual entry UI
    from flask import Flask, request, Response, render_template_string, jsonify
    
    app = create_webhook_app()
    
    # Add manual METAR entry page
    MANUAL_UI_HTML = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>WX Sniper</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; background: #1a1a2e; color: #eee; }
            h1 { color: #00ff88; }
            textarea { width: 100%; height: 120px; font-family: monospace; font-size: 14px; background: #16213e; color: #eee; border: 1px solid #0f3460; padding: 10px; }
            button { background: #00ff88; color: #1a1a2e; border: none; padding: 15px 30px; font-size: 18px; cursor: pointer; margin-top: 10px; }
            button:hover { background: #00cc6a; }
            .result { margin-top: 20px; padding: 15px; background: #16213e; border-radius: 5px; }
            .success { border-left: 4px solid #00ff88; }
            .error { border-left: 4px solid #ff4444; }
            .status { margin-top: 20px; padding: 10px; background: #0f3460; border-radius: 5px; font-size: 12px; }
            pre { white-space: pre-wrap; word-wrap: break-word; }
        </style>
    </head>
    <body>
        <h1>🌡️ WX Sniper</h1>
        <p>Paste METAR from Leidos SMS below:</p>
        
        <textarea id="metar" placeholder="KSFO 281853Z 29012KT 10SM FEW020 SCT200 17/08 A3012 RMK AO2 SLP203 T01720083 10189 20156 58010"></textarea>
        <br>
        <button onclick="submitMetar()">🎯 Process METAR</button>
        
        <div id="result"></div>
        
        <div class="status">
            <strong>Status:</strong> <span id="status">Ready</span><br>
            <strong>Last Update:</strong> <span id="lastUpdate">-</span><br>
            <strong>Mode:</strong> {{ "DRY RUN" if dry_run else "🔴 LIVE TRADING" }}
        </div>
        
        <script>
            async function submitMetar() {
                const metar = document.getElementById('metar').value;
                document.getElementById('status').textContent = 'Processing...';
                
                try {
                    const response = await fetch('/api/process', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({metar: metar})
                    });
                    const data = await response.json();
                    
                    let html = '<div class="result ' + (data.success ? 'success' : 'error') + '">';
                    html += '<strong>' + (data.success ? '✅ Processed' : '❌ Error') + '</strong><br>';
                    html += '<pre>' + JSON.stringify(data, null, 2) + '</pre>';
                    html += '</div>';
                    
                    document.getElementById('result').innerHTML = html;
                    document.getElementById('status').textContent = 'Ready';
                    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
                } catch (e) {
                    document.getElementById('result').innerHTML = '<div class="result error">Error: ' + e + '</div>';
                    document.getElementById('status').textContent = 'Error';
                }
            }
        </script>
    </body>
    </html>
    '''
    
    @app.route('/')
    def index():
        return render_template_string(MANUAL_UI_HTML, dry_run=dry_run)
    
    @app.route('/api/process', methods=['POST'])
    def api_process():
        """API endpoint for manual METAR submission"""
        data = request.get_json()
        metar_text = data.get('metar', '').strip()
        
        if not metar_text:
            return jsonify({'success': False, 'error': 'No METAR provided'})
        
        try:
            results = sniper.process_metar(metar_text)
            
            return jsonify({
                'success': True,
                'metar': metar_text[:60] + '...',
                'trades': [
                    {
                        'station': r.signal.station,
                        'type': r.signal.bracket_type,
                        'temp': r.signal.temperature_f,
                        'market': r.market_ticker,
                        'executed': r.success,
                        'error': r.error
                    }
                    for r in results
                ]
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    
    @app.route('/api/status')
    def api_status():
        """Get current bot status"""
        state = sniper.state
        return jsonify({
            'time': datetime.now(timezone.utc).isoformat(),
            'dry_run': dry_run,
            'stations': {
                station: {
                    'high': state.get_state(station).tracked_high_f,
                    'low': state.get_state(station).tracked_low_f
                }
                for station in STATIONS.keys()
            }
        })
    
    # Background polling thread
    def poll_aviation_weather():
        """Poll aviationweather.gov with adaptive rate - fast during synoptic windows"""
        from config import (
            POLL_INTERVAL_NORMAL_SECONDS,
            POLL_INTERVAL_HOT_SECONDS,
            HOT_WINDOW_START_MINUTE,
            HOT_WINDOW_END_MINUTE,
            SYNOPTIC_HOURS_UTC
        )
        
        print(f"[POLL] Starting adaptive polling:")
        print(f"       Normal: every {POLL_INTERVAL_NORMAL_SECONDS}s")
        print(f"       Hot window (:51-:58 on synoptic hours): every {POLL_INTERVAL_HOT_SECONDS}s")
        
        while True:
            try:
                now = datetime.now(timezone.utc)
                hour = now.hour
                minute = now.minute
                
                # Check if we're in a hot window (synoptic hour, minutes 51-58)
                is_synoptic_hour = hour in SYNOPTIC_HOURS_UTC
                is_hot_window = is_synoptic_hour and HOT_WINDOW_START_MINUTE <= minute <= HOT_WINDOW_END_MINUTE
                
                if is_hot_window:
                    interval = POLL_INTERVAL_HOT_SECONDS
                    window_label = "🔥 HOT"
                else:
                    interval = POLL_INTERVAL_NORMAL_SECONDS
                    window_label = "💤 normal"
                
                # Fetch new METARs
                new_metars = poller.check_for_new_metars()
                
                for metar in new_metars:
                    # Check if this is a synoptic METAR (has 6-hour groups)
                    if poller.is_synoptic_metar(metar):
                        print(f"[POLL] 📊 SYNOPTIC METAR detected: {metar.station}")
                        results = sniper.process_metar(metar.raw_text)
                        
                        for r in results:
                            if r.success:
                                print(f"[POLL] ✅ Trade executed: {r.market_ticker}")
                            else:
                                print(f"[POLL] ⏭️ No trade: {r.error}")
                    else:
                        # Only log during hot window to reduce noise
                        if is_hot_window:
                            print(f"[POLL] Regular METAR (no 6hr groups): {metar.station}")
                
                # Log status during hot window
                if is_hot_window and minute != getattr(poll_aviation_weather, '_last_log_minute', -1):
                    print(f"[POLL] {window_label} | {now.strftime('%H:%M:%SZ')} | polling every {interval}s")
                    poll_aviation_weather._last_log_minute = minute
                        
            except Exception as e:
                print(f"[POLL] Error: {e}")
            
            time.sleep(interval)
    
    poll_thread = Thread(target=poll_aviation_weather, daemon=True)
    poll_thread.start()
    
    print("\n✅ Hybrid server starting...")
    print(f"   - Web UI: http://{host}:{port}/")
    print(f"   - SMS Webhook: http://{host}:{port}/sms/webhook")
    print(f"   - Auto-polling aviationweather.gov every {AVIATIONWEATHER_POLL_INTERVAL_SECONDS}s")
    print("\nWaiting for METARs...\n")
    
    app.run(host=host, port=port, debug=False, threaded=True)


def run_manual_test(metar_text: str = None, dry_run: bool = True):
    """
    Run a manual test with a METAR string
    
    Usage: python main.py test --metar "KSFO 281853Z ..."
    """
    print("=" * 60)
    print("WX SNIPER - MANUAL TEST")
    print("=" * 60)
    
    if not metar_text:
        # Use sample METAR
        metar_text = "KSFO 281853Z 29012KT 10SM FEW020 SCT200 17/08 A3012 RMK AO2 SLP203 T01720083 10189 20156 58010"
        print(f"Using sample METAR (no --metar provided)")
    
    print(f"\nMETAR: {metar_text}")
    
    sniper = WXSniper(dry_run=dry_run)
    results = sniper.process_metar(metar_text)
    
    print(f"\n{'='*50}")
    print("RESULTS")
    print("=" * 50)
    
    for result in results:
        status = "✓ SUCCESS" if result.success else "✗ FAILED"
        print(f"\n{status}")
        print(f"  Station: {result.signal.station}")
        print(f"  Type: {result.signal.bracket_type}")
        print(f"  Temp: {result.signal.temperature_f}°F")
        print(f"  Market: {result.market_ticker}")
        if result.price_paid_cents:
            print(f"  Price: {result.price_paid_cents}¢")
        if result.contracts:
            print(f"  Contracts: {result.contracts}")
        if result.error:
            print(f"  Note: {result.error}")
    
    print(f"\n{sniper.state.get_summary()}")


def run_ticker_discovery():
    """
    Discover Kalshi ticker formats by querying the API
    """
    print("=" * 60)
    print("WX SNIPER - TICKER DISCOVERY")
    print("=" * 60)
    
    client = KalshiClient()
    
    # Test connection
    print("\n[1] Testing API connection...")
    try:
        balance = client.get_balance()
        print(f"✓ Connected! Balance: ${balance.get('balance', 0) / 100:.2f}")
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        print("\nMake sure your API credentials are correct in config.py")
        return
    
    # Discover events for each station
    print("\n[2] Discovering temperature markets...")
    
    for station, config in STATIONS.items():
        high_ticker = config.get("kalshi_high_ticker")
        low_ticker = config.get("kalshi_low_ticker")
        
        print(f"\n{'='*50}")
        print(f"Station: {station} ({config['name']})")
        print(f"{'='*50}")
        
        if high_ticker:
            print(f"\n  HIGH series: {high_ticker}")
            try:
                events = client.get_events(series_ticker=high_ticker, limit=2)
                print(f"  Found {len(events)} events")
                
                for event in events:
                    print(f"\n    EVENT: {event.get('event_ticker')}")
                    print(f"    Title: {event.get('title')}")
                    
                    markets = event.get('markets', [])
                    print(f"    Markets: {len(markets)}")
                    
                    if markets:
                        print(f"\n    Sample market tickers:")
                        for m in markets[:5]:
                            ticker = m.get('ticker')
                            subtitle = m.get('yes_sub_title', '')
                            bid = m.get('yes_bid', '-')
                            ask = m.get('yes_ask', '-')
                            print(f"      {ticker}")
                            print(f"        {subtitle} | bid:{bid}¢ ask:{ask}¢")
                            
            except Exception as e:
                print(f"  Error: {e}")
        
        if low_ticker:
            print(f"\n  LOW series: {low_ticker}")
            try:
                events = client.get_events(series_ticker=low_ticker, limit=2)
                print(f"  Found {len(events)} events")
            except Exception as e:
                print(f"  Error: {e}")


def run_send_sms_test(station: str = "KSFO"):
    """
    Test sending SMS to Leidos
    """
    print("=" * 60)
    print("WX SNIPER - SMS TEST")
    print("=" * 60)
    
    if not TWILIO_ACCOUNT_SID:
        print("\n✗ Twilio not configured!")
        print("Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_PHONE_NUMBER in config.py")
        return
    
    sms_client = TwilioSMSClient()
    
    print(f"\nSending METAR request for {station}...")
    sid = sms_client.send_metar_request(station)
    
    if sid:
        print(f"✓ Message sent! SID: {sid}")
        print("\nNow wait for the response via webhook.")
        print("Make sure your webhook server is running!")
    else:
        print("✗ Failed to send message")


def main():
    parser = argparse.ArgumentParser(description="WX Sniper - Temperature Market Trading Bot")
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Hybrid server (main mode)
    server_parser = subparsers.add_parser("server", help="Run hybrid server (webhook + polling + UI)")
    server_parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    server_parser.add_argument("--port", type=int, default=5000, help="Port to bind to")
    server_parser.add_argument("--live", action="store_true", help="Enable live trading (default: dry run)")
    
    # Alias for server
    webhook_parser = subparsers.add_parser("webhook", help="Alias for 'server'")
    webhook_parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    webhook_parser.add_argument("--port", type=int, default=5000, help="Port to bind to")
    webhook_parser.add_argument("--live", action="store_true", help="Enable live trading")
    
    # Manual test
    test_parser = subparsers.add_parser("test", help="Run manual test")
    test_parser.add_argument("--metar", help="METAR string to test")
    test_parser.add_argument("--live", action="store_true", help="Enable live trading")
    
    # Ticker discovery
    discover_parser = subparsers.add_parser("discover", help="Discover Kalshi ticker formats")
    
    # SMS test
    sms_parser = subparsers.add_parser("sms", help="Test SMS sending")
    sms_parser.add_argument("--station", default="KSFO", help="Station to request")
    
    # Status
    status_parser = subparsers.add_parser("status", help="Show current state")
    
    # Poll test
    poll_parser = subparsers.add_parser("poll", help="Test aviationweather.gov polling")
    
    args = parser.parse_args()
    
    if args.command in ["server", "webhook"]:
        run_hybrid_server(
            host=args.host,
            port=args.port,
            dry_run=not args.live
        )
    
    elif args.command == "test":
        run_manual_test(
            metar_text=args.metar,
            dry_run=not args.live
        )
    
    elif args.command == "discover":
        run_ticker_discovery()
    
    elif args.command == "sms":
        run_send_sms_test(args.station)
    
    elif args.command == "status":
        state = StateManager()
        print(state.get_summary())
    
    elif args.command == "poll":
        run_poll_test()
    
    else:
        parser.print_help()
        print("\n" + "=" * 60)
        print("QUICK START")
        print("=" * 60)
        print("""
1. First, discover the Kalshi ticker format:
   python main.py discover

2. Test with a sample METAR:
   python main.py test

3. Test aviationweather.gov polling:
   python main.py poll

4. Run the hybrid server (production):
   python main.py server --port 5000
   
   Add --live to enable real trading (default is dry run)
   
   Then open http://localhost:5000 to paste METARs manually,
   or forward SMS to your Twilio webhook.
        """)


def run_poll_test():
    """Test aviationweather.gov polling"""
    print("=" * 60)
    print("WX SNIPER - POLL TEST")
    print("=" * 60)
    
    from aviation_weather import AviationWeatherPoller
    
    poller = AviationWeatherPoller()
    
    print("\nFetching METARs from aviationweather.gov...")
    metars = poller.fetch_all_stations()
    
    print(f"\nFetched {len(metars)} METARs:\n")
    
    for metar in metars:
        is_synoptic = poller.is_synoptic_metar(metar)
        icon = "📊 SYNOPTIC" if is_synoptic else "   regular"
        time_str = metar.observation_time.strftime("%d %H:%MZ") if metar.observation_time else "???"
        
        print(f"{icon} {metar.station} @ {time_str}")
        print(f"         {metar.raw_text[:70]}...")
        
        if is_synoptic:
            # Parse and show 6-hour groups
            parsed = parse_metar(metar.raw_text)
            if parsed.six_hour_max_f:
                print(f"         6HR MAX: {parsed.six_hour_max_f:.1f}°F -> {parsed.six_hour_max_f_rounded}°F")
            if parsed.six_hour_min_f:
                print(f"         6HR MIN: {parsed.six_hour_min_f:.1f}°F -> {parsed.six_hour_min_f_rounded}°F")
        print()


if __name__ == "__main__":
    main()
