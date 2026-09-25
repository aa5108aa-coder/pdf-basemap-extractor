from flask import Flask, render_template_string, request, send_file
import pymupdf
import io
import re

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>都市計畫圖底圖萃取工具</title>
    <style>
        body { font-family: "Microsoft JhengHei", sans-serif; padding: 20px; text-align: center; background-color: #f5f7fa; color: #333; }
        .container { max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        h1 { font-size: 24px; margin-bottom: 10px; }
        p { color: #666; margin-bottom: 20px; line-height: 1.6; text-align: left; }
        .btn { padding: 10px 20px; margin: 15px 5px; cursor: pointer; background: #007bff; color: white; border: none; border-radius: 4px; font-size: 16px; transition: background 0.3s; }
        .btn:hover { background: #0056b3; }
        input[type="file"] { margin: 10px 0; padding: 10px; border: 1px solid #ccc; border-radius: 4px; width: 100%; box-sizing: border-box; }
        .checkbox-container { text-align: left; background: #fff3cd; padding: 15px; border-radius: 4px; border: 1px solid #ffeeba; margin-bottom: 15px; }
        #loading { display: none; color: #007bff; font-weight: bold; margin-top: 15px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>都市計畫圖底圖萃取工具</h1>
        <p>請上傳原始的都市計畫圖 PDF 檔案，系統將自動進行以下處理：<br>
        1. 移除各類使用分區的彩色圖塊<br>
        2. 移除圖面粗框線，保留底層的街道、建築與地籍細線<br>
        3. 保留可選取的「樓層數字文字層」，以利後續 3D 建模程式讀取</p>
        
        <form action="/convert" method="post" enctype="multipart/form-data" onsubmit="document.getElementById('loading').style.display='block'; document.getElementById('submit-btn').disabled=true;">
            <input type="file" name="pdf_file" accept=".pdf" required />
            
            <div class="checkbox-container">
                <label>
                    <input type="checkbox" name="remove_chinese" value="yes"> 
                    <b>完全去除中文標籤 (如地名、學校名稱)</b>
                </label>
                <div style="font-size: 12px; color: #856404; margin-top: 5px;">
                    備註：由於原始檔案中的中文標籤為向量線條，若勾選此選項，標籤底下的部分地圖線條會連帶被移除而產生微小斷點。
                </div>
            </div>
            
            <button type="submit" id="submit-btn" class="btn">開始轉換</button>
        </form>
        
        <div id="loading">處理中，大約需要 15 ~ 30 秒，請耐心等候...</div>
    </div>
</body>
</html>
"""

def process_stream(stream):
    tokens = stream.split()
    stroke_c = (0,0,0)
    fill_c = (0,0,0)
    current_w = 0.0
    
    i = 0
    L = len(tokens)
    while i < L:
        tok = tokens[i]
        
        if tok == b'RG' and i >= 3:
            try: stroke_c = (float(tokens[i-3]), float(tokens[i-2]), float(tokens[i-1]))
            except: pass
        elif tok == b'rg' and i >= 3:
            try: fill_c = (float(tokens[i-3]), float(tokens[i-2]), float(tokens[i-1]))
            except: pass
        elif tok in (b'K', b'k') and i >= 4:
            c = (1,0,0) if tok == b'K' else (0,0,1)
            if tok == b'K': stroke_c = c
            else: fill_c = c
        elif tok in (b'G', b'g') and i >= 1:
            try:
                v = float(tokens[i-1])
                if tok == b'G': stroke_c = (v,v,v)
                else: fill_c = (v,v,v)
            except: pass
        elif tok == b'w' and i >= 1:
            try: current_w = float(tokens[i-1])
            except: pass
            
        if tok in (b'S', b's'):
            if max(stroke_c)-min(stroke_c)>0.05 or sum(stroke_c)>2.8 or current_w > 0.1: tok = b'n'
        elif tok in (b'f', b'F', b'f*'):
            if max(fill_c)-min(fill_c)>0.05 or sum(fill_c)>2.8: tok = b'n'
        elif tok in (b'B', b'B*', b'b', b'b*'):
            sc = max(stroke_c)-min(stroke_c)>0.05 or sum(stroke_c)>2.8 or current_w > 0.1
            fc = max(fill_c)-min(fill_c)>0.05 or sum(fill_c)>2.8
            if sc and fc: tok = b'n'
            elif sc: tok = b'f' if tok in (b'B', b'b') else b'f*'
            elif fc: tok = b'S' if tok in (b'B', b'B*') else b's'
            
        tokens[i] = tok
        i += 1

    return b' '.join(tokens)

def process_pdf(input_bytes, remove_chinese):
    doc = pymupdf.open(stream=input_bytes, filetype="pdf")
    page = doc[0]
    
    # 1. 移除文字與大型標籤
    blocks = page.get_text('dict')['blocks']
    for b in blocks:
        if 'lines' in b:
            for l in b['lines']:
                for span in l['spans']:
                    text = span['text'].strip()
                    if not text: continue
                    
                    should_redact = False
                    if span['size'] >= 5.0:
                        should_redact = True
                    elif remove_chinese and not text.isascii():
                        should_redact = True
                        
                    if should_redact:
                        page.add_redact_annot(span['bbox'], fill=None)
                        
    # 若選擇移除中文，連同向量圖形(graphics=1)一起清空，才能消滅化為圖形的中文字
    page.apply_redactions(images=0, graphics=1 if remove_chinese else 0, text=0)
    
    # 2. 移除色彩區塊與粗框線 (保留 BT/ET 文字指令)
    page.clean_contents()
    xref = page.get_contents()[0]
    stream = doc.xref_stream(xref)
    new_stream = process_stream(stream)
    doc.update_stream(xref, new_stream)
    
    # 3. 移除不必要邊緣區塊
    page.draw_rect(pymupdf.Rect(0, 0, 1729, 110), color=None, fill=(1, 1, 1))
    page.draw_rect(pymupdf.Rect(1000, 1500, 1729, 2126), color=None, fill=(1, 1, 1))
    page.draw_rect(pymupdf.Rect(0, 2050, 200, 2126), color=None, fill=(1, 1, 1))
    
    out_pdf_bytes = doc.write(deflate=True, garbage=4)
    doc.close()
    return out_pdf_bytes

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/convert", methods=["POST"])
def convert():
    if "pdf_file" not in request.files:
        return "No file uploaded", 400
        
    file = request.files["pdf_file"]
    if file.filename == "":
        return "No file selected", 400
        
    remove_chinese = request.form.get("remove_chinese") == "yes"
        
    try:
        input_bytes = file.read()
        output_bytes = process_pdf(input_bytes, remove_chinese)
        
        return send_file(
            io.BytesIO(output_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name="extracted_basemap_final.pdf"
        )
    except Exception as e:
        return f"Error processing PDF: {str(e)}", 500

if __name__ == "__main__":
    print("啟動網頁伺服器中... 請在瀏覽器開啟 http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
