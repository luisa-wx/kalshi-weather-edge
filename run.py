#!/usr/bin/env python3
"""
Simple entry point for DigitalOcean App Platform
"""
import os
from main import create_app

if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", 8080))
    print(f"Starting server on port {port}...")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
