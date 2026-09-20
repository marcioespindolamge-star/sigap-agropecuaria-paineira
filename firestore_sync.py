"""Sincronização opcional do banco SQLite do SIGAP com Cloud Firestore.

O SQLite continua sendo o banco local do SIGAP. Quando credenciais do Firebase
estão configuradas no ambiente, uma cópia das tabelas é mantida no Firestore.
Nenhuma chave privada é gravada no GitHub.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:
    firebase_admin = None
    credentials = None
    firestore = None

PROJECT_ID = "sigap-agropecuaria-paineira"
TABELAS_SIGAP = (
    "animais", "nascimentos", "pesagens", "sanidade", "medicamentos",
    "movimentacoes", "frigorificos", "compradores", "vendas", "venda_itens",
    "campos", "inseminacoes", "entouramentos", "protocolos_iatf", "config",
)

def firestore_habilitado():
    return bool(firebase_admin and (
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        or os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    ))

def _cliente():
    if not firestore_habilitado():
        return None
    if not firebase_admin._apps:
        cred_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
        if cred_json:
            cred = credentials.Certificate(json.loads(cred_json))
        else:
            cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred, {"projectId": PROJECT_ID})
    return firestore.client()

def _normalizar(valor):
    if isinstance(valor, (str, int, float, bool)) or valor is None:
        return valor
    return str(valor)

def salvar_documento(colecao, documento_id, dados):
    db = _cliente()
    if db is None:
        return False
    payload = {str(k): _normalizar(v) for k, v in dict(dados).items()}
    payload["_sigap_atualizado_em"] = datetime.now(timezone.utc).isoformat()
    db.collection(str(colecao)).document(str(documento_id)).set(payload, merge=True)
    return True

def excluir_documento(colecao, documento_id):
    db = _cliente()
    if db is None:
        return False
    db.collection(str(colecao)).document(str(documento_id)).delete()
    return True

def sincronizar_sqlite(db_path):
    """Espelha as tabelas do SIGAP no Firestore. Retorna False se desativado."""
    cloud = _cliente()
    if cloud is None:
        return False

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        existentes = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        agora = datetime.now(timezone.utc).isoformat()

        for tabela in TABELAS_SIGAP:
            if tabela not in existentes:
                continue
            rows = conn.execute(f'SELECT rowid AS _rowid_, * FROM "{tabela}"').fetchall()
            ids_atuais = set()
            batch = cloud.batch()
            operacoes = 0

            for row in rows:
                dados = dict(row)
                doc_id = str(dados.get("id") or dados.get("_rowid_"))
                ids_atuais.add(doc_id)
                dados.pop("_rowid_", None)
                dados = {str(k): _normalizar(v) for k, v in dados.items()}
                dados["_sigap_atualizado_em"] = agora
                ref = cloud.collection(tabela).document(doc_id)
                batch.set(ref, dados)
                operacoes += 1
                if operacoes >= 400:
                    batch.commit()
                    batch = cloud.batch()
                    operacoes = 0

            # Sincronização segura: apenas cria/atualiza documentos no Firestore.
            # Exclusões locais não são propagadas automaticamente para a nuvem.
            if operacoes:
                batch.commit()

        cloud.collection("_sigap").document("estado").set({
            "ultima_sincronizacao": agora,
            "origem": "sqlite-local",
            "projeto": PROJECT_ID,
        }, merge=True)
        return True
    finally:
        conn.close()
