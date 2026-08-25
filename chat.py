#!/usr/bin/env python3
"""
NemApi Test Client - Contexte Incremental

Script simple sans dependances externes pour tester la gestion du contexte
avec l'API NemApi en mode incremental.

A chaque nouvelle question, le script envoie TOUT l'historique a l'API.
L'API (en mode incremental_context=True) doit extraire seulement le dernier
message user et l'envoyer au provider, tout en conservant le contexte natif
cote interface web.

Usage:
    python3 chat.py

Configurer d'abord:
    1. Lancer le proxy: python3 proxy.py
    2. Charger l'extension Firefox et selectionner les onglets fournisseurs
    3. Dans ce script, choisir le provider et model
"""

import json
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# Configuration par defaut (modifiable)
HOST = "127.0.0.1"
PORT = 8080
BASE_URL = f"http://{HOST}:{PORT}"

# Modeles disponibles (doit correspondre a proxy.py)
# Maintenant compatible OpenAI : seul le model est requis, le provider est deduit
AVAILABLE_MODELS = {
    "deepseek": ["deepseek-chat", "chat"],
    "qwen": ["qwen-plus", "qwen2.5-plus", "plus"],
    "claude": ["claude-sonnet", "claude-3-sonnet", "claude-3-haiku", "sonnet"],
    "gemini": ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-pro", "flash"],
}

# Liste plate de tous les models pour l'affichage
ALL_MODELS = []
for provider, models in AVAILABLE_MODELS.items():
    for model in models:
        ALL_MODELS.append({"name": model, "provider": provider})
        ALL_MODELS.append({"name": f"{provider}/{model}", "provider": provider})


def print_header(text):
    """Affiche un header style."""
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}")


def print_message(role, content, provider=None):
    """Affiche un message de facon lisible."""
    role_color = {
        "user": "\033[94m",      # Bleu
        "assistant": "\033[92m", # Vert
        "system": "\033[93m",   # Jaune
    }
    color = role_color.get(role, "\033[0m")
    reset = "\033[0m"
    
    prefix = f"{color}[{role.upper()}]{reset}"
    if provider:
        prefix += f" ({provider})"
    
    print(f"{prefix}: {content}")


def send_request(model, messages, stream=False, provider=None):
    """
    Envoie une requete a l'API NemApi.
    
    Args:
        model: Modele a utiliser (ex: "qwen-plus" ou "qwen/qwen-plus")
        messages: Liste de dictionnaires avec 'role' et 'content'
        stream: Si True, utilise le streaming (non implemente ici pour simplicite)
        provider: Fournisseur (optionnel, deduit du model si non specifie)
    
    Returns:
        str: La reponse de l'assistant
    """
    url = f"{BASE_URL}/v1/chat/completions"
    
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    if provider:
        payload["provider"] = provider
    
    headers = {
        "Content-Type": "application/json",
    }
    
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers=headers, method="POST")
    
    try:
        with urlopen(req, timeout=300) as response:
            if response.status != 200:
                body = response.read().decode("utf-8")
                try:
                    error_data = json.loads(body)
                    error_msg = error_data.get("error", {}).get("message", body)
                except:
                    error_msg = body
                raise Exception(f"HTTP {response.status}: {error_msg}")
            
            body = response.read().decode("utf-8")
            data = json.loads(body)
            
            # Extraire la reponse
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"]["content"]
            else:
                return ""
    
    except HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            error_data = json.loads(body)
            error_msg = error_data.get("error", {}).get("message", body)
        except:
            error_msg = body
        raise Exception(f"HTTP {e.code}: {error_msg}")
    except URLError as e:
        raise Exception(f"Impossible de se connecter au proxy: {e.reason}")
    except json.JSONDecodeError as e:
        raise Exception(f"Reponse JSON invalide: {e}")


def check_proxy_status():
    """Verifie si le proxy est en ligne."""
    try:
        url = f"{BASE_URL}/status"
        req = Request(url, method="GET")
        with urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("status") == "ready"
    except:
        return False


def select_model():
    """Permet a l'utilisateur de selectionner un modele."""
    print_header("Selection du Modele")
    print("\nModeles disponibles (le provider est deduit automatiquement):")
    
    # Afficher les modeles par provider
    for provider, models in AVAILABLE_MODELS.items():
        print(f"\n  📌 {provider}:")
        for model in models:
            print(f"     - {model}  (ou {provider}/{model})")
    
    print("\n  Exemples de modeles valides:")
    print("     qwen-plus, deepseek-chat, claude-sonnet, gemini-2.5-flash")
    print("     qwen/qwen-plus, deepseek/deepseek-chat, etc.")
    
    while True:
        try:
            choice = input("\nEntrez le nom du modele: ").strip()
            if not choice:
                # Default to first model
                provider = list(AVAILABLE_MODELS.keys())[0]
                model = AVAILABLE_MODELS[provider][0]
                print(f"Modele par defaut: {model} ({provider})")
                return model
            
            # Check if the model exists (either short name or full name)
            model_found = False
            for item in ALL_MODELS:
                if item["name"] == choice:
                    model_found = True
                    print(f"\nSelection: {choice} (provider: {item['provider']})")
                    return choice
            
            # If not in the list, still accept it (proxy will validate)
            print(f"\nSelection: {choice} (le proxy validera le modele)")
            return choice
            
        except KeyboardInterrupt:
            print("\nAnnule.")
            sys.exit(0)


def main():
    """Boucle principale de chat."""
    print_header("NemApi Test Client - Contexte Incremental")
    print("\nCe script teste le mode incremental_context de l'API.")
    print("A chaque nouvelle question, TOUT l'historique est envoye a l'API.")
    print("L'API doit extraire seulement le dernier message user.")
    
    # Verifier le proxy
    print("\nVerification du proxy...")
    if not check_proxy_status():
        print("⚠️  Le proxy n'est pas pret. Assurez-vous que:")
        print("   1. python3 proxy.py est lance")
        print("   2. L'extension Firefox est chargee")
        print("   3. Les onglets fournisseurs sont selectionnes dans l'admin")
        print("\nAppuyez sur Entree pour continuer (ou Ctrl+C pour annuler)...")
        input()
    else:
        print("✅ Proxy est pret!")
    
    # Selection du modele
    model = select_model()
    
    # Extraire le provider du modele pour l'affichage
    model_display = model
    provider_display = "?"
    if "/" in model:
        provider_display = model.split("/")[0]
        model_display = model.split("/")[1] if len(model.split("/")) > 1 else model
    else:
        # Try to find provider from model name
        for item in ALL_MODELS:
            if item["name"] == model:
                provider_display = item["provider"]
                break
    
    # Historique de la conversation
    messages = []
    
    # Message systeme optionnel
    system_prompt = input("\nMessage systeme optionnel (laissez vide pour aucun): ").strip()
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
        print_message("system", system_prompt)
    
    print(f"\n💬 Demarrage de la conversation avec modele: {model}")
    print(f"   Provider deduit: {provider_display}")
    print("   Tapez vos messages. Utilisez 'quit' ou 'exit' pour finir.")
    print("   L'historique complet est envoye a chaque requete!")
    print("   Le mode incremental_context de l'API doit gerer ca.")
    
    # Boucle de chat
    while True:
        try:
            user_input = input("\nVous: ").strip()
            
            if not user_input:
                continue
            
            if user_input.lower() in ('quit', 'exit', 'q'):
                print("\nFin de la conversation.")
                break
            
            # Ajouter le message user a l'historique
            messages.append({"role": "user", "content": user_input})
            print_message("user", user_input)
            
            # Envoyer TOUT l'historique a l'API
            print(f"\n  → Envoi de {len(messages)} messages a l'API...")
            print(f"     Modele: {model}")
            
            start_time = time.time()
            
            try:
                response = send_request(model, messages, stream=False)
                elapsed = time.time() - start_time
                
                print_message("assistant", response, provider_display)
                print(f"\n  ✅ Reponse recue en {elapsed:.2f}s")
                
                # Ajouter la reponse a l'historique
                messages.append({"role": "assistant", "content": response})
                
            except Exception as e:
                print(f"\n  ❌ Erreur: {e}")
                print(f"\n  ⚠️  Ceci peut etre du au fait que:")
                print(f"     - Le provider {provider_display} n'a pas d'onglet selectionne")
                print(f"     - L'extension n'est pas connectee")
                print(f"     - Le modele '{model}' n'est pas reconnu")
                print(f"     - L'interface web du provider a change")
                
                # Ne pas ajouter l'erreur a l'historique
                messages.pop()  # Retirer le dernier message user
                
        except KeyboardInterrupt:
            print("\n\nFin de la conversation.")
            break
    
    # Afficher un resume
    print_header("Resume de la Conversation")
    print(f"\nModele: {model} (provider: {provider_display})")
    print(f"Nombre total de messages echanges: {len(messages)}")
    print(f"Historique complet:")
    
    for i, msg in enumerate(messages, 1):
        role = msg["role"]
        content = msg["content"]
        print(f"\n  {i}. [{role.upper()}] {content[:100]}{'...' if len(content) > 100 else ''}")
    
    print("\nTest termine!")


if __name__ == "__main__":
    main()
