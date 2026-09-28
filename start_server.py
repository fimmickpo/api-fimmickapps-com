#!/usr/bin/env python3
import sys, os
sys.path.insert(0, '/Users/ollama/api_root')
os.chdir('/Users/ollama/api_root')
import uvicorn
uvicorn.run('app:app', host='127.0.0.1', port=8001, log_level='info')