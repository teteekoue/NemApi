#!/usr/bin/env python3
"""
Faux Proxy de Debug pour Qwen Coder

Ce proxy fictif enregistre toutes les requetes recues sur /v1/chat/completions
 dans un fichier debug_requests.json pour analyser le format des donnees envoyees
 par Qwen Coder (notamment si le system prompt est bien inclus).

Usage:
    python3 debug_proxy.py

Les requetes sont sauvegardees dans debug_requests.json dans le meme dossier.
Expose l'endpoint sur http://127.0.0.1:8081/v1/chat/completions
"""

import json
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

HOST, PORT = "127.0.0.1", 8080
LOG_FILE = "debug_requests.json"

# Verrou pour l'ecriture concurrentielle
log_lock = threading.Lock()


class DebugHandler(BaseHTTPRequestHandler):
    server_version = "DebugProxy/1.0"

    def log_message(self, _fmt, *_args):
        pass  # Desactive les logs du serveur HTTP

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        size = int(self.headers.get("Content-Length") or 0)
        if size <= 0:
            return {}
        return json.loads(self.rfile.read(size).decode("utf-8") or "{}")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        path = self.path
        try:
            if path == "/v1/chat/completions":
                self._handle_chat_completion()
            elif path == "/v1/models":
                # Retourne la liste des modeles disponibles
                self._json({
                    "object": "list",
                    "data": [
                        {"id": "deepseek-chat", "object": "model", "owned_by": "deepseek"},
                        {"id": "qwen-plus", "object": "model", "owned_by": "qwen"},
                        {"id": "claude-sonnet", "object": "model", "owned_by": "claude"},
                        {"id": "gemini-2.5-flash", "object": "model", "owned_by": "gemini"}
                    ]
                })
            else:
                self._json({"error": {"message": "not found", "type": "not_found"}}, 404)
        except Exception as e:
            self._json({"error": {"message": str(e), "type": "server_error"}}, 500)

    def _handle_chat_completion(self):
        """Traite une requete /v1/chat/completions et l'enregistre."""
        body = self._read_json()
        
        # Creer l'enregistrement de debug
        request_record = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
            "headers": dict(self.headers),
            "body": body,
            "summary": self._extract_summary(body)
        }
        
        # Sauvegarder dans le fichier
        self._save_request(request_record)
        
        # Generer une reponse factice compatible OpenAI
        response = self._generate_fake_response(body)
        
        # Si le client veut du streaming
        if body.get("stream", False):
            self._sse_response(body, response)
        else:
            self._json(response)

    def _extract_summary(self, body):
        """Extraire un resume lisible de la requete."""
        messages = body.get("messages", [])
        model = body.get("model", "unknown")
        stream = body.get("stream", False)
        
        roles = [m.get("role") for m in messages if isinstance(m, dict)]
        has_system = "system" in roles
        
        return {
            "model": model,
            "stream": stream,
            "message_count": len(messages),
            "has_system_prompt": has_system,
            "message_roles": roles,
            "first_message_content": messages[0].get("content", "")[:100] if messages else ""
        }

    def _save_request(self, record):
        """Sauvegarde une requete dans le fichier de log."""
        with log_lock:
            try:
                # Lire les requetes existantes
                try:
                    with open(LOG_FILE, "r", encoding="utf-8") as f:
                        requests = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    requests = []
                
                # Ajouter la nouvelle requete
                requests.append(record)
                
                # Garder seulement les 100 dernieres pour eviter un fichier trop gros
                if len(requests) > 100:
                    requests = requests[-100:]
                
                # Ecrire
                with open(LOG_FILE, "w", encoding="utf-8") as f:
                    json.dump(requests, f, ensure_ascii=False, indent=2)
                
                # Afficher un resume dans la console
                summary = record.get("summary", {})
                print(f"\n[{record['timestamp'][:19]}] NOUVELLE REQUETE")
                print(f"  Model: {summary.get('model', '?')}")
                print(f"  Stream: {summary.get('stream', False)}")
                print(f"  Messages: {summary.get('message_count', 0)}")
                print(f"  System prompt: {'OUI' if summary.get('has_system_prompt') else 'NON'}")
                print(f"  Roles: {summary.get('message_roles', [])}")
                if summary.get('message_count', 0) > 0:
                    print(f"  1er message: {summary.get('first_message_content', '')[:80]}...")
            except Exception as e:
                print(f"[ERREUR] Impossible de sauvegarder: {e}")

    def _generate_fake_response(self, body):
        """Genere une reponse factice mais realiste."""
        messages = body.get("messages", [])
        model = body.get("model", "deepseek-chat")
        
        # Compter les tokens approximatifs
        def count_tokens(text):
            return len(str(text or "").split())
        
        prompt_tokens = sum(count_tokens(m.get("content")) for m in messages if isinstance(m, dict))
        
        return {
            "id": f"chatcmpl-{int(time.time())}-{hash(model) % 10000:04d}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Ceci est une reponse factice du proxy de debug. Votre requete a ete enregistree dans debug_requests.json pour analyse."
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 20,
                "total_tokens": prompt_tokens + 20
            }
        }

    def _sse_response(self, body, full_response):
        """Envoie une reponse en streaming SSE."""
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        
        content = full_response["choices"][0]["message"]["content"]
        chat_id = full_response["id"]
        model = full_response["model"]
        created = full_response["created"]
        
        # Premier chunk avec le role
        first_chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"role": "assistant", "content": ""},
                "finish_reason": None
            }]
        }
        self.wfile.write(f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()
        
        # Envoyer le contenu par morceaux
        for i in range(0, len(content), 10):
            chunk = content[i:i+10]
            delta_chunk = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": chunk},
                    "finish_reason": None
                }]
            }
            self.wfile.write(f"data: {json.dumps(delta_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.05)
        
        # Fin
        last_chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": "stop"
            }]
        }
        self.wfile.write(f"data: {json.dumps(last_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    server = ThreadingHTTPServer((HOST, PORT), DebugHandler)
    print(f"""
=============================================================
 Debug Proxy pour Qwen Coder
=============================================================
 Ecoute sur: http://{HOST}:{PORT}/v1/chat/completions

 Configuration pour Qwen Coder:
   - URL: http://127.0.0.1:8081/v1
   - Modele: deepseek-chat

 Les requetes seront sauvegardees dans: {LOG_FILE}

 Appuyez sur Ctrl+C pour arreter le serveur.
=============================================================
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\nArret du proxy de debug.")
        print(f"Fichier de log: {LOG_FILE}")


if __name__ == "__main__":
    main()
