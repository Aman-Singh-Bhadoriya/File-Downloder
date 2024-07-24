import os
import pandas as pd
import requests
from pathlib import Path
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, render_template, jsonify
import tkinter as tk
from tkinter import filedialog
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads/'
socketio = SocketIO(app, async_mode='eventlet')

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

stop_requested = False

def download_file(url, output_dir, timeout=60):
    global stop_requested
    try:
        url = url.strip()
        url = re.sub(r'^\d+\)\s*', '', url)
        url = re.sub(r'[<>"]', '', url)
        if not url.startswith(('http://', 'https://')):
            return url, False
        
        start_time = time.time()
        try:
            response = requests.get(url, stream=True, timeout=timeout)
            response.raise_for_status()
            
            filename = url.split("/")[-1]
            file_path = output_dir / filename

            base, extension = Path(filename).stem, Path(filename).suffix
            counter = 1
            while file_path.exists():
                file_path = output_dir / f"{base}_{counter}{extension}"
                counter += 1

            with open(file_path, 'wb') as file:
                for chunk in response.iter_content(chunk_size=8192):
                    if stop_requested:
                        return url, False
                    file.write(chunk)
            return url, True
        except requests.exceptions.RequestException:
            return url, False
        finally:
            elapsed_time = time.time() - start_time
            if elapsed_time > timeout:
                print(f"URL processing time exceeded {timeout} seconds for {url}.")
    except Exception:
        return url, False

def read_file(file_path):
    try:
        data = pd.read_excel(file_path, engine='openpyxl')
        return data
    except Exception:
        pass

    try:
        for encoding in ['ISO-8859-1', 'latin1']:
            try:
                data = pd.read_html(file_path, flavor='html5lib', encoding=encoding)[0]
                return data
            except Exception:
                pass
    except Exception:
        pass

    try:
        data = pd.read_csv(file_path, encoding='ISO-8859-1')
        return data
    except Exception:
        pass

    try:
        with open(file_path, 'r', encoding='ISO-8859-1') as file:
            content = file.read()
            urls = re.findall(r'(https?://\S+)', content)
            return pd.DataFrame({'Document Path': urls})
    except Exception:
        pass

    return None

@app.route('/', methods=['GET', 'POST'])
def index():
    global stop_requested
    if request.method == 'POST':
        file = request.files['file']
        column_name = request.form['column_name']
        output_path = request.form['output_path']
        
        if not os.path.exists(output_path):
            os.makedirs(output_path)

        file_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
        file.save(file_path)

        data = read_file(file_path)
        if data is None or column_name not in data.columns:
            return render_template('index.html', message="Failed to read file or column not found.")

        urls = data[column_name].dropna()

        global stop_requested
        stop_requested = False

        socketio.start_background_task(target=process_urls, urls=urls, output_path=output_path, column_name=column_name, data=data)

        return render_template('index.html', message="Processing started. Check the progress below:")

    return render_template('index.html')

def process_urls(urls, output_path, column_name, data):
    global stop_requested
    success_count = 0
    failure_count = 0
    successful_urls = []
    failed_urls = []

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(download_file, url, Path(output_path)): url for url in urls}
        for future in as_completed(futures):
            if stop_requested:
                break
            url = futures[future]
            try:
                url, success = future.result()
                if success:
                    success_count += 1
                    successful_urls.append(url)
                else:
                    failure_count += 1
                    failed_urls.append(url)
            except Exception:
                failure_count += 1
                failed_urls.append(url)
            
            socketio.emit('update', {'success': success_count, 'fail': failure_count})

    remaining_urls = data[~data[column_name].isin(successful_urls)]
    remaining_file_path = os.path.join(output_path, 'remaining_urls.xlsx')
    remaining_urls.to_excel(remaining_file_path, index=False)

    failed_file_path = os.path.join(output_path, 'failed_urls.xlsx')
    pd.DataFrame({'Failed URLs': failed_urls}).to_excel(failed_file_path, index=False)
    
    socketio.emit('finished', {'success': success_count, 'fail': failure_count})

@app.route('/choose_directory')
def choose_directory():
    root = tk.Tk()
    root.withdraw()
    folder_selected = filedialog.askdirectory()
    root.destroy()
    return jsonify(directory=folder_selected)

if __name__ == '__main__':
    socketio.run(app, debug=True)
