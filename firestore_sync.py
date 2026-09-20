"""Sincronização opcional do banco SQLite do SIGAP com Cloud Firestore.

O SQLite continua sendo o banco local do SIGAP. Quando credenciais do Firebase
estão configuradas no ambiente, uma cópia das tabelas é mantida no Firestore.
Nenhuma chave privada é gravada no GitHub.
"""
import json
import os
import sqlite3
import hashlib
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

def _hash_dados(dados):
    """Hash estável do conteúdo local para evitar reenvios desnecessários."""
    bruto = json.dumps(dados, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()

def sincronizar_sqlite(db_path):
    """Sincroniza somente registros novos ou alterados. Não propaga exclusões."""
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
        enviados = 0

        for tabela in TABELAS_SIGAP:
            if tabela not in existentes:
                continue

            rows = conn.execute(f'SELECT rowid AS _rowid_, * FROM "{tabela}"').fetchall()
            batch = cloud.batch()
            operacoes = 0

            for row in rows:
                dados = dict(row)
                doc_id = str(dados.get("id") or dados.get("_rowid_"))
                dados.pop("_rowid_", None)
                dados = {str(k): _normalizar(v) for k, v in dados.items()}
                hash_local = _hash_dados(dados)

                ref = cloud.collection(tabela).document(doc_id)
                atual = ref.get()
                if atual.exists:
                    remoto = atual.to_dict() or {}
                    if remoto.get("_sigap_hash") == hash_local:
                        continue

                payload = dict(dados)
                payload["_sigap_hash"] = hash_local
                payload["_sigap_atualizado_em"] = agora
                batch.set(ref, payload, merge=True)
                operacoes += 1
                enviados += 1

                if operacoes >= 400:
                    batch.commit()
                    batch = cloud.batch()
                    operacoes = 0

            # Segurança mantida: exclusões locais não são propagadas automaticamente.
            if operacoes:
                batch.commit()

        cloud.collection("_sigap").document("estado").set({
            "ultima_sincronizacao": agora,
            "origem": "sqlite-local",
            "projeto": PROJECT_ID,
            "documentos_enviados": enviados,
        }, merge=True)
        return True
    finally:
        conn.close()


def restaurar_firestore_para_sqlite(db_path):
    """Carrega o espelho do Firestore para um SQLite já inicializado.

    Uso destinado ao ambiente online (Render), onde o disco local é efêmero.
    Não apaga dados do Firestore e não remove registros locais.
    """
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
        conn.execute("PRAGMA foreign_keys=OFF")

        # Migração de compatibilidade do banco efêmero do Render.
        # A versão atual do SIGAP usa esta coluna nas telas/consultas de matrizes.
        if "animais" in existentes:
            colunas_animais = {
                r[1] for r in conn.execute('PRAGMA table_info("animais")').fetchall()
            }
            if "diu_aplicado" not in colunas_animais:
                conn.execute(
                    'ALTER TABLE "animais" ADD COLUMN "diu_aplicado" INTEGER DEFAULT 0'
                )
                conn.commit()

        total = 0

        for tabela in TABELAS_SIGAP:
            if tabela not in existentes:
                continue

            colunas = [
                r[1] for r in conn.execute(f'PRAGMA table_info("{tabela}")').fetchall()
            ]
            permitidas = set(colunas)
            for snap in cloud.collection(tabela).stream():
                dados = snap.to_dict() or {}
                dados = {
                    k: v for k, v in dados.items()
                    if k in permitidas and not str(k).startswith("_sigap_")
                }
                if "id" in permitidas and "id" not in dados:
                    try:
                        dados["id"] = int(snap.id)
                    except (TypeError, ValueError):
                        pass
                if not dados:
                    continue

                nomes = list(dados.keys())
                marcas = ",".join("?" for _ in nomes)
                cols = ",".join(f'"{n}"' for n in nomes)
                sql = f'INSERT OR REPLACE INTO "{tabela}" ({cols}) VALUES ({marcas})'
                conn.execute(sql, tuple(dados[n] for n in nomes))
                total += 1

        conn.commit()
        return total
    finally:
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        conn.close()
