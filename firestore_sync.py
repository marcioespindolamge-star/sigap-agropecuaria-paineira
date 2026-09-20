"""Integração opcional do SIGAP com Google Cloud Firestore.

Não altera o banco SQLite local. O Firestore só é ativado quando as
credenciais são fornecidas por variável de ambiente, evitando gravar
chaves privadas no GitHub.
"""
import os
from datetime import datetime, timezone

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    firebase_admin = None
    credentials = None
    firestore = None


def firestore_habilitado():
    return bool(
        firebase_admin
        and (
            os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            or os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
        )
    )


def _cliente():
    if not firestore_habilitado():
        return None

    if not firebase_admin._apps:
        json_cred = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
        if json_cred:
            import json
            cred = credentials.Certificate(json.loads(json_cred))
            firebase_admin.initialize_app(
                cred, {"projectId": "sigap-agropecuaria-paineira"}
            )
        else:
            cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(
                cred, {"projectId": "sigap-agropecuaria-paineira"}
            )
    return firestore.client()


def salvar_documento(colecao, documento_id, dados):
    """Salva/atualiza uma cópia no Firestore sem interferir no SQLite."""
    db = _cliente()
    if db is None:
        return False
    payload = dict(dados)
    payload["_sigap_atualizado_em"] = datetime.now(timezone.utc).isoformat()
    db.collection(str(colecao)).document(str(documento_id)).set(
        payload, merge=True
    )
    return True


def excluir_documento(colecao, documento_id):
    db = _cliente()
    if db is None:
        return False
    db.collection(str(colecao)).document(str(documento_id)).delete()
    return True
