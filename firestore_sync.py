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
    """Espelha rapidamente os registros locais no Firestore, sem propagar exclusões.

    Evita uma leitura remota por registro. Isso é importante no Render, onde a
    sincronização ocorre dentro da requisição e precisa terminar antes do timeout.
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
        agora = datetime.now(timezone.utc).isoformat()
        enviados = 0
        batch = cloud.batch()
        operacoes = 0

        for tabela in TABELAS_SIGAP:
            if tabela not in existentes:
                continue

            rows = conn.execute(f'SELECT rowid AS _rowid_, * FROM "{tabela}"').fetchall()
            for row in rows:
                dados = dict(row)
                doc_id = str(dados.get("id") or dados.get("_rowid_"))
                dados.pop("_rowid_", None)
                dados = {str(k): _normalizar(v) for k, v in dados.items()}

                payload = dict(dados)
                payload["_sigap_hash"] = _hash_dados(dados)
                payload["_sigap_atualizado_em"] = agora

                ref = cloud.collection(tabela).document(doc_id)
                batch.set(ref, payload, merge=True)
                operacoes += 1
                enviados += 1

                if operacoes >= 400:
                    batch.commit()
                    batch = cloud.batch()
                    operacoes = 0

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


# Rota complementar de edição de venda.
# Registrada quando o Flask do SIGAP é criado, sem alterar o app.py nem a rotina
# de restauração Firestore -> SQLite.
def _registrar_edicao_venda_sigap():
    try:
        from flask import Flask, request, render_template, redirect, url_for, flash, abort
    except Exception:
        return

    original_init = Flask.__init__
    if getattr(original_init, "_sigap_edicao_venda", False):
        return

    def init_com_edicao(self, *args, **kwargs):
        original_init(self, *args, **kwargs)

        def editar_venda_realizada(venda_id):
            db_path = os.environ.get(
                "PAINEIRA_DB",
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados", "rebanho.db")
            )
            conn = sqlite3.connect(db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            venda = conn.execute("SELECT * FROM vendas WHERE id=?", (venda_id,)).fetchone()
            if not venda:
                conn.close()
                abort(404)
            itens = conn.execute(
                "SELECT * FROM venda_itens WHERE venda_id=? ORDER BY id", (venda_id,)
            ).fetchall()

            if request.method == "POST":
                f = request.form
                def numero(valor, padrao=0.0):
                    try:
                        texto = str(valor or "").strip()
                        if "," in texto:
                            texto = texto.replace(".", "").replace(",", ".")
                        return float(texto)
                    except Exception:
                        return float(padrao or 0)

                data = (f.get("data") or venda["data"] or "").strip()
                forma = (f.get("forma_calculo") or venda["forma_calculo"] or "kg").strip()
                if forma not in ("kg", "unitario"):
                    forma = "kg"
                valor_ref = numero(f.get("valor_referencia"), venda["valor_referencia"])

                # Animais e pesos permanecem exatamente como foram gravados na venda.
                total_peso = round(sum(float(i["peso"] or 0) for i in itens), 2)
                novos_valores = []
                for item in itens:
                    valor = (
                        round(float(item["peso"] or 0) * valor_ref, 2)
                        if forma == "kg" else round(valor_ref, 2)
                    )
                    novos_valores.append((valor, item["id"]))
                bruto = round(sum(v for v, _ in novos_valores), 2)
                fundo = round(bruto * 0.015, 2)
                liquido = round(bruto - fundo, 2)

                conn.execute("""UPDATE vendas SET
                    data=?, banco=?, agencia=?, conta_corrente=?, cpf_titular=?,
                    forma_calculo=?, valor_referencia=?, total_peso=?, total_bruto=?,
                    percentual_fundo=1.5, fundo_rural=?, total_liquido=?, observacoes=?,
                    comprador_nome=?, comprador_nome_fantasia=?, comprador_cnpj=?,
                    comprador_cep=?, comprador_cidade=?, comprador_endereco=?,
                    comprador_observacoes=?
                    WHERE id=?""",
                    (
                        data, (f.get("banco") or "").strip(),
                        (f.get("agencia") or "").strip(),
                        (f.get("conta_corrente") or "").strip(),
                        (f.get("cpf_titular") or "").strip(),
                        forma, valor_ref, total_peso, bruto, fundo, liquido,
                        (f.get("observacoes") or "").strip(),
                        (f.get("comprador_nome") or "").strip(),
                        (f.get("comprador_nome_fantasia") or "").strip(),
                        (f.get("comprador_cnpj") or "").strip(),
                        (f.get("comprador_cep") or "").strip(),
                        (f.get("comprador_cidade") or "").strip(),
                        (f.get("comprador_endereco") or "").strip(),
                        (f.get("comprador_observacoes") or "").strip(),
                        venda_id,
                    )
                )
                for valor, item_id in novos_valores:
                    conn.execute(
                        "UPDATE venda_itens SET valor_individual=? WHERE id=? AND venda_id=?",
                        (valor, item_id, venda_id),
                    )
                conn.commit()
                conn.close()
                flash("Venda atualizada. Os animais vendidos não foram alterados.", "ok")
                return redirect(url_for("relatorio_venda", venda_id=venda_id))

            conn.close()
            return render_template("editar_venda.html", venda=venda, itens=itens)

        self.add_url_rule(
            "/movimentacoes/venda/<int:venda_id>/editar",
            endpoint="editar_venda_realizada",
            view_func=editar_venda_realizada,
            methods=["GET", "POST"],
        )

    init_com_edicao._sigap_edicao_venda = True
    Flask.__init__ = init_com_edicao

_registrar_edicao_venda_sigap()
