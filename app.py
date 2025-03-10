from flask import Flask, render_template, request, session, jsonify, redirect, url_for
from celery import Celery
from flask_caching import Cache
import os
import googleapiclient.discovery
import googleapiclient.errors
import datetime
import re
import time
from authlib.integrations.flask_client import OAuth

# Configuração Celery para fila de tarefas
app = Flask(__name__)

# Usar Redis do Render (ou localhost para desenvolvimento)
app.config['CELERY_BROKER_URL'] = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
app.config['CELERY_RESULT_BACKEND'] = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)

# Configuração de Cache
cache = Cache(app, config={'CACHE_TYPE': 'SimpleCache'})

# Configuração OAuth YouTube
oauth = OAuth(app)
app.config['GOOGLE_CLIENT_ID'] = os.getenv('GOOGLE_CLIENT_ID', 'seu-client-id')
app.config['GOOGLE_CLIENT_SECRET'] = os.getenv('GOOGLE_CLIENT_SECRET', 'seu-client-secret')

google = oauth.register(
    name='google',
    client_id=app.config['GOOGLE_CLIENT_ID'],
    client_secret=app.config['GOOGLE_CLIENT_SECRET'],
    access_token_url='https://accounts.google.com/o/oauth2/token',
    authorize_url='https://accounts.google.com/o/oauth2/auth',
    api_base_url='https://www.googleapis.com/',
    client_kwargs={'scope': 'https://www.googleapis.com/auth/youtube'}
)

# Lista de chaves de API do YouTube
API_KEYS = [
    "AIzaSyAkcsGlsM-5pSd_JzmhQaKFoHjoMw1uzpQ",  # Primeira chave
    "AIzaSyCL1uz9bpnw-6hDFgBtGzqTJID5UYLxKig",  # Segunda chave
    "AIzaSyAOGfr_EWvLPUbhV-cMJV7tdKOZ72qVdf8"    # Terceira chave
]

# Variável de controle para chave de API atual
current_api_key_index = 0

# Gerenciamento global para resultados da busca
search_results = {}

def get_youtube_service():
    global current_api_key_index
    api_service_name = "youtube"
    api_version = "v3"

    # Usar a chave atual
    api_key = API_KEYS[current_api_key_index]

    try:
        return googleapiclient.discovery.build(
            api_service_name, api_version, developerKey=api_key)
    except googleapiclient.errors.HttpError as e:
        # Se receber erro de limite excedido (403), tenta a próxima chave
        if e.resp.status == 403:
            current_api_key_index = (current_api_key_index + 1) % len(API_KEYS)
            api_key = API_KEYS[current_api_key_index]
            return googleapiclient.discovery.build(
                api_service_name, api_version, developerKey=api_key)
        else:
            raise

@celery.task(bind=True)
def search_videos_task(self, keywords, min_comments, playlist_id, days_back):
    start_time = time.time()
    timeout = 60 * 15  # 15 minutos em segundos
    youtube = get_youtube_service()
    
    # Adicionar progresso real
    total_keywords = len(keywords_list)
    for i, keyword in enumerate(keywords_list):
        self.update_state(state='PROGRESS', meta={'current': i+1, 'total': total_keywords})

    # Preparar lista de palavras-chave
    keywords_list = [keyword.strip() for keyword in keywords.split(',')]

    all_videos = []
    next_page_token = None
    max_results_per_page = 50  # Aumentado para maximizar resultados no tempo limite

    # Data atual
    current_time = datetime.datetime.now()
    thirty_days_ago = current_time - datetime.timedelta(days=30)
    thirty_days_ago_str = thirty_days_ago.strftime('%Y-%m-%dT%H:%M:%SZ')

    # Conjunto para gerenciar IDs de vídeos já vistos para evitar duplicatas
    seen_video_ids = set()

    # Conjunto para armazenar IDs de vídeos na playlist (se fornecido)
    playlist_video_ids = set()
    if playlist_id and playlist_id.strip():
        try:
            # Obter vídeos da playlist
            playlist_items_request = youtube.playlistItems().list(
                part="contentDetails",
                playlistId=playlist_id,
                maxResults=50
            )
            playlist_response = playlist_items_request.execute()

            while True:
                for item in playlist_response["items"]:
                    playlist_video_ids.add(item["contentDetails"]["videoId"])

                # Verificar se há mais páginas na playlist
                if "nextPageToken" in playlist_response:
                    try:
                        playlist_items_request = youtube.playlistItems().list(
                            part="contentDetails",
                            playlistId=playlist_id,
                            maxResults=50,
                            pageToken=playlist_response["nextPageToken"]
                        )
                        playlist_response = playlist_items_request.execute()
                    except googleapiclient.errors.HttpError:
                        # Se ocorrer um erro, tenta com outra chave de API
                        youtube = get_youtube_service()
                        playlist_items_request = youtube.playlistItems().list(
                            part="contentDetails",
                            playlistId=playlist_id,
                            maxResults=50,
                            pageToken=playlist_response["nextPageToken"]
                        )
                        playlist_response = playlist_items_request.execute()
                else:
                    break
        except googleapiclient.errors.HttpError as e:
            print(f"Erro ao acessar a playlist: {e}")

    # Buscar vídeos para cada palavra-chave até atingir timeout
    while time.time() - start_time < timeout:
        for keyword in keywords_list:
            try:
                search_request = youtube.search().list(
                    part="snippet",
                    q=keyword,
                    type="video",
                    maxResults=max_results_per_page,
                    pageToken=next_page_token
                )
                search_response = search_request.execute()

                video_ids = [item["id"]["videoId"] for item in search_response["items"] 
                            if item["id"]["videoId"] not in seen_video_ids]

                # Atualizar conjunto de IDs já vistos
                for video_id in video_ids:
                    seen_video_ids.add(video_id)

                # Buscar detalhes dos vídeos (estatísticas)
                if video_ids:
                    try:
                        videos_request = youtube.videos().list(
                            part="statistics,snippet",
                            id=",".join(video_ids)
                        )
                        videos_response = videos_request.execute()

                        # Buscar comentários recentes
                        for video in videos_response["items"]:
                            video_id = video["id"]

                            # Pular se já estiver na playlist
                            if video_id in playlist_video_ids:
                                continue

                            # Verificar comentários
                            try:
                                comments_request = youtube.commentThreads().list(
                                    part="snippet",
                                    videoId=video_id,
                                    maxResults=100,
                                    order="time",
                                    searchTerms=""
                                )
                                comments_response = comments_request.execute()

                                # Contar comentários dos últimos 30 dias
                                recent_comments = 0
                                if "items" in comments_response:
                                    for comment in comments_response["items"]:
                                        comment_date = comment["snippet"]["topLevelComment"]["snippet"]["publishedAt"]
                                        if comment_date > thirty_days_ago_str:
                                            recent_comments += 1

                                # Adicionar vídeo à lista se tiver comentários suficientes
                                if recent_comments >= int(min_comments):
                                    all_videos.append({
                                        "id": video_id,
                                        "title": video["snippet"]["title"],
                                        "thumbnail": video["snippet"]["thumbnails"]["medium"]["url"],
                                        "channel": video["snippet"]["channelTitle"],
                                        "comments": recent_comments,
                                        "viewCount": video["statistics"].get("viewCount", "0"),
                                        "likeCount": video["statistics"].get("likeCount", "0"),
                                        "url": f"https://www.youtube.com/watch?v={video_id}"
                                    })
                            except googleapiclient.errors.HttpError:
                                # Tentar nova chave de API se for erro de limite
                                try:
                                    youtube = get_youtube_service()
                                    comments_request = youtube.commentThreads().list(
                                        part="snippet",
                                        videoId=video_id,
                                        maxResults=100,
                                        order="time",
                                        searchTerms=""
                                    )
                                    comments_response = comments_request.execute()

                                    # Contar comentários dos últimos 30 dias
                                    recent_comments = 0
                                    if "items" in comments_response:
                                        for comment in comments_response["items"]:
                                            comment_date = comment["snippet"]["topLevelComment"]["snippet"]["publishedAt"]
                                            if comment_date > thirty_days_ago_str:
                                                recent_comments += 1

                                    # Adicionar vídeo à lista se tiver comentários suficientes
                                    if recent_comments >= int(min_comments):
                                        all_videos.append({
                                            "id": video_id,
                                            "title": video["snippet"]["title"],
                                            "thumbnail": video["snippet"]["thumbnails"]["medium"]["url"],
                                            "channel": video["snippet"]["channelTitle"],
                                            "comments": recent_comments,
                                            "viewCount": video["statistics"].get("viewCount", "0"),
                                            "likeCount": video["statistics"].get("likeCount", "0"),
                                            "url": f"https://www.youtube.com/watch?v={video_id}"
                                        })
                                except:
                                    # Se ainda falhar, ignorar este vídeo
                                    continue
                    except googleapiclient.errors.HttpError:
                        # Se ocorrer um erro, tenta com outra chave de API
                        youtube = get_youtube_service()
                        continue

                # Verificar se há mais páginas
                if "nextPageToken" in search_response:
                    next_page_token = search_response["nextPageToken"]
                else:
                    next_page_token = None
                    break

            except googleapiclient.errors.HttpError:
                # Se ocorrer um erro, tenta com outra chave de API
                youtube = get_youtube_service()
                continue

            # Verificar se atingiu o limite de tempo
            if time.time() - start_time >= timeout:
                break

        if next_page_token is None or time.time() - start_time >= timeout:
            break

    # Atualizar resultados globais
    search_results[search_id] = all_videos

    print(f"Busca concluída em {time.time() - start_time} segundos. Encontrados {len(all_videos)} vídeos.")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/search', methods=['POST'])
def search():
    keywords = request.form.get('keywords', '')
    min_comments = request.form.get('min_comments', '0')
    playlist_id = request.form.get('playlist_id', '')
    days_back = int(request.form.get('days_back', 30))  # Novo campo no formulário
    
    # Validação básica dos dados de entrada
    if not keywords:
        return render_template('index.html', error="Por favor, insira pelo menos uma palavra-chave")
    
    # Iniciar tarefa Celery
    task = search_videos_task.apply_async(args=(keywords, min_comments, playlist_id, days_back))
    session['task_id'] = task.id
    
    return render_template('loading.html', 
                          search_id=task.id, 
                          keywords=keywords, 
                          min_comments=min_comments, 
                          playlist_id=playlist_id)

@app.route('/results/<search_id>')
def results(search_id):
    if search_id not in search_results:
        return redirect(url_for('index'))

    videos = search_results[search_id]
    keywords = request.args.get('keywords', '')
    min_comments = request.args.get('min_comments', '0')
    playlist_id = request.args.get('playlist_id', '')

    return render_template('results.html', 
                          videos=videos, 
                          keywords=keywords, 
                          min_comments=min_comments, 
                          playlist_id=playlist_id)

@app.route('/check_search_status/<search_id>')
def check_search_status(search_id):
    is_complete = search_id in search_results
    total_found = len(search_results.get(search_id, [])) if is_complete else 0
    return jsonify({'complete': is_complete, 'total_found': total_found})

@app.route('/add_to_playlist', methods=['POST'])
def add_to_playlist():
    if 'youtube_token' not in session:
        return jsonify({'success': False, 'message': 'Faça login no YouTube primeiro'})
    
    video_id = request.form.get('video_id')
    playlist_id = request.form.get('playlist_id')
    
    if not video_id or not playlist_id:
        return jsonify({'success': False, 'message': 'Parâmetros inválidos'})
    
    try:
        # Usar token do usuário
        youtube = googleapiclient.discovery.build(
            "youtube", "v3", credentials=session['youtube_token'])
        
        # Adicionar vídeo à playlist
        add_video_request = youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id
                    }
                }
            }
        )
        add_video_request.execute()

        return jsonify({'success': True})
    except googleapiclient.errors.HttpError as e:
        error_message = f"Erro ao adicionar vídeo: {str(e)}"
        return jsonify({'success': False, 'message': error_message})

if __name__ == '__main__':
    app.run(debug=True)