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
            <strong>Polling:</strong> Normal=60s, Hot window (:52-:02)=5s
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
    
    @app.route('/api/discover')
    def api_discover():
        """Discover Kalshi temperature market tickers"""
        from kalshi_client import KalshiClient
        
        results = {}
        try:
            client = KalshiClient()
            
            # Search for temperature markets
            search_terms = ['HIGHTSFO', 'HIGHTLV', 'HIGHNY', 'KXHIGH', 'temperature', 'temp']
            
            for term in search_terms:
                try:
                    events = client.get_events(series_ticker=term, limit=5)
                    if events:
                        results[term] = []
                        for event in events[:3]:
                            event_info = {
                                'event_ticker': event.get('event_ticker'),
                                'title': event.get('title'),
                                'markets': []
                            }
                            # Get markets for this event
                            markets = event.get('markets', [])
                            for m in markets[:5]:
                                event_info['markets'].append({
                                    'ticker': m.get('ticker'),
                                    'subtitle': m.get('yes_sub_title', m.get('subtitle')),
                                    'yes_bid': m.get('yes_bid'),
                                    'yes_ask': m.get('yes_ask')
                                })
                            results[term].append(event_info)
                except Exception as e:
                    results[term] = f"Error: {str(e)}"
            
            # Also try to get markets directly
            try:
                all_markets = client.get_markets(limit=50)
                temp_markets = [m for m in all_markets if 'temp' in m.get('ticker', '').lower() or 'high' in m.get('ticker', '').lower()]
                results['direct_market_search'] = [
                    {'ticker': m.get('ticker'), 'title': m.get('title', m.get('yes_sub_title', ''))} 
                    for m in temp_markets[:20]
                ]
            except Exception as e:
                results['direct_market_search'] = f"Error: {str(e)}"
                
        except Exception as e:
            results['error'] = str(e)
        
        return jsonify(results)
    
    # Note: /health route is defined in twilio_handler.py's create_webhook_app()
    
    @app.route('/prices/<city>')
    def check_prices(city):
        """
        Check current prices for a city's high temp market
        Usage: /prices/sfo or /prices/las
        """
        from kalshi_client import KalshiClient
        from config import STATIONS
        from datetime import datetime
        
        city_map = {
            'sfo': 'KSFO',
            'las': 'KLAS',
            'nyc': 'KNYC',
            'phl': 'KPHL',
            'chi': 'KMDW',
            'mia': 'KMIA',
            'aus': 'KAUS',
            'den': 'KDEN',
            'sea': 'KSEA',
            'dca': 'KDCA',
            'msy': 'KMSY',
            'lax': 'KLAX'
        }
        
        station = city_map.get(city.lower())
        if not station:
            return jsonify({'error': f'Unknown city: {city}', 'valid': list(city_map.keys())})
        
        station_config = STATIONS.get(station)
        if not station_config:
            return jsonify({'error': f'Station {station} not configured'})
        
        # Get today's date in Kalshi format
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        
        tz_map = {
            'KSFO': 'America/Los_Angeles', 'KLAS': 'America/Los_Angeles',
            'KSEA': 'America/Los_Angeles', 'KLAX': 'America/Los_Angeles',
            'KDEN': 'America/Denver', 'KAUS': 'America/Chicago',
            'KMDW': 'America/Chicago', 'KMSY': 'America/Chicago',
            'KNYC': 'America/New_York', 'KPHL': 'America/New_York',
            'KMIA': 'America/New_York', 'KDCA': 'America/New_York'
        }
        
        local_tz = ZoneInfo(tz_map.get(station, 'America/New_York'))
        local_now = datetime.now(local_tz)
        market_date = local_now.strftime("%y%b%d").upper()
        
        series_ticker = station_config.get('kalshi_high_ticker', '')
        
        result = {
            'station': station,
            'city': city.upper(),
            'series_ticker': series_ticker,
            'market_date': market_date,
            'local_time': local_now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            'brackets': []
        }
        
        try:
            client = KalshiClient()
            # Query by series_ticker to find open markets
            markets = client.get_markets(series_ticker=series_ticker, status='open')
            
            if not markets:
                result['error'] = f'No open markets found for series {series_ticker}'
                return jsonify(result)
            
            # Get event_ticker from first market
            event_ticker = markets[0].get('event_ticker', '')
            result['event_ticker'] = event_ticker
            result['total_markets'] = len(markets)
            
            for m in markets:
                bracket = {
                    'ticker': m.get('ticker'),
                    'subtitle': m.get('yes_sub_title') or m.get('subtitle'),
                    'yes_bid': m.get('yes_bid'),
                    'yes_bid_dollars': m.get('yes_bid_dollars'),
                    'yes_ask': m.get('yes_ask'),
                    'yes_ask_dollars': m.get('yes_ask_dollars'),
                    'no_bid': m.get('no_bid'),
                    'no_bid_dollars': m.get('no_bid_dollars'),
                    'no_ask': m.get('no_ask'),
                    'no_ask_dollars': m.get('no_ask_dollars'),
                    'last_price': m.get('last_price'),
                    'last_price_dollars': m.get('last_price_dollars'),
                    'volume': m.get('volume')
                }
                result['brackets'].append(bracket)
            
            # Sort by subtitle (temperature range)
            result['brackets'].sort(key=lambda x: x.get('subtitle', ''))
            
        except Exception as e:
            result['error'] = str(e)
            import traceback
            result['traceback'] = traceback.format_exc()
        
        return jsonify(result)
    
    # Background polling thread - SMART approach
    def poll_aviation_weather():
        """
        Smart polling strategy:
        1. At :50 - Pre-scan Kalshi for opportunities
        2. At :52 - Start polling ONLY stations with opportunities
        3. Stop polling each station once synoptic METAR received
        4. At :02 - Stop all polling for this cycle
        """
        from smart_poller import SmartPoller
        from config import (
            PRE_SCAN_MINUTE,
            HOT_WINDOW_START_MINUTE,
            HOT_WINDOW_END_MINUTE,
            POLL_INTERVAL_HOT_SECONDS,
            POLL_INTERVAL_NORMAL_SECONDS,
            OPPORTUNITY_THRESHOLD_CENTS
        )
        
        print(f"[POLL] Starting SMART polling:")
        print(f"       Pre-scan Kalshi at :{PRE_SCAN_MINUTE:02d}")
        print(f"       Hot window :{HOT_WINDOW_START_MINUTE:02d}-:{HOT_WINDOW_END_MINUTE:02d} (synoptic hours)")
        print(f"       Opportunity threshold: {OPPORTUNITY_THRESHOLD_CENTS}¢")
        
        smart_poller = SmartPoller(sniper.kalshi, threshold_cents=OPPORTUNITY_THRESHOLD_CENTS)
        
        # Track cycle state
        current_scan = None
        last_pre_scan_hour = -1
        last_status_minute = -1
        
        while True:
            try:
                now = datetime.now(timezone.utc)
                hour = now.hour
                minute = now.minute
                
                is_synoptic_hour = hour in SYNOPTIC_HOURS_UTC
                
                # Determine what phase we're in
                if is_synoptic_hour and minute == PRE_SCAN_MINUTE and last_pre_scan_hour != hour:
                    # PRE-SCAN PHASE: Scan Kalshi for opportunities
                    print(f"\n[POLL] ━━━ PRE-SCAN PHASE ━━━")
                    current_scan = smart_poller.pre_scan_kalshi()
                    last_pre_scan_hour = hour
                    smart_poller.reset_cycle()
                    
                    if current_scan.stations_to_watch:
                        print(f"[POLL] Will watch: {', '.join(current_scan.stations_to_watch)}")
                    else:
                        print(f"[POLL] No opportunities - will skip this cycle")
                
                # Check if we're in hot window
                # Handle wrap-around for :52 to :02 (next hour)
                if HOT_WINDOW_END_MINUTE < HOT_WINDOW_START_MINUTE:
                    # Window crosses hour boundary (e.g., :52 to :02)
                    is_hot_window = is_synoptic_hour and (
                        minute >= HOT_WINDOW_START_MINUTE or minute <= HOT_WINDOW_END_MINUTE
                    )
                else:
                    is_hot_window = is_synoptic_hour and (
                        HOT_WINDOW_START_MINUTE <= minute <= HOT_WINDOW_END_MINUTE
                    )
                
                if is_hot_window and current_scan and current_scan.stations_to_watch:
                    # HOT POLLING PHASE: Poll targeted stations
                    remaining = current_scan.stations_to_watch - smart_poller.stations_done
                    
                    if remaining:
                        if minute != last_status_minute:
                            print(f"[POLL] 🔥 HOT | {now.strftime('%H:%M:%S')}Z | {len(remaining)} stations remaining")
                            last_status_minute = minute
                        
                        for station in list(remaining):
                            result = smart_poller.fetch_metar(station)
                            
                            if result:
                                raw_text, obs_time = result
                                
                                # Check if NEW metar
                                last_time = smart_poller.last_obs_time.get(station)
                                if last_time and obs_time and obs_time <= last_time:
                                    continue
                                
                                if obs_time:
                                    smart_poller.last_obs_time[station] = obs_time
                                
                                # Check if synoptic
                                if smart_poller.is_synoptic_metar(raw_text):
                                    print(f"[POLL] ✅ SYNOPTIC: {station} @ {obs_time.strftime('%H:%M')}Z")
                                    smart_poller.stations_done.add(station)
                                    
                                    # Process and trade!
                                    try:
                                        results = sniper.process_metar(raw_text)
                                        for r in results:
                                            if r.success:
                                                print(f"[POLL] 💰 TRADE: {r.market_ticker} @ {r.price_paid_cents}¢")
                                            elif r.error:
                                                print(f"[POLL] ⏭️ {station}: {r.error}")
                                    except Exception as e:
                                        print(f"[POLL] Error processing {station}: {e}")
                        
                        time.sleep(POLL_INTERVAL_HOT_SECONDS)
                        continue  # Don't sleep again at bottom
                    
                    else:
                        # All stations done!
                        if minute != last_status_minute:
                            print(f"[POLL] ✅ All synoptic METARs received for this cycle")
                            last_status_minute = minute
                
                elif is_synoptic_hour and minute > HOT_WINDOW_END_MINUTE and minute < PRE_SCAN_MINUTE:
                    # Between cycles - just log occasionally
                    if minute != last_status_minute:
                        print(f"[POLL] 💤 {now.strftime('%H:%M:%S')}Z | Waiting for next synoptic")
                        last_status_minute = minute
                
                # Normal sleep
                time.sleep(POLL_INTERVAL_NORMAL_SECONDS if not is_hot_window else POLL_INTERVAL_HOT_SECONDS)
                        
            except Exception as e:
                print(f"[POLL] Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)  # Wait before retrying
    
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
        print(f"       Hot window (:52-:02 on synoptic hours): every {POLL_INTERVAL_HOT_SECONDS}s")
        
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
