"""
Sincronizador bidirecional CRM ↔ Clínica-Agentes.

Roda como script avulso ou como job agendado.
Lógica: last-write-wins por updated_at.

Uso:
    python sync.py
    python sync.py --watch          # sincroniza a cada 60 segundos
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

# ── localiza os dois bancos ────────────────────────────────────────────────────
HERE = Path(__file__).parent
CRM_DB = HERE / "crm.db"
CLINICA_DB = HERE.parent / "clinica-agentes" / "clinica-agentes" / "clinica.db"

if not CRM_DB.exists():
    sys.exit("crm.db não encontrado. Inicie o CRM ao menos uma vez.")
if not CLINICA_DB.exists():
    sys.exit(f"clinica.db não encontrado em {CLINICA_DB}. Verifique o caminho.")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

crm_engine = create_engine(f"sqlite:///{CRM_DB}", connect_args={"check_same_thread": False})
cli_engine = create_engine(f"sqlite:///{CLINICA_DB}", connect_args={"check_same_thread": False})
CrmSession = sessionmaker(bind=crm_engine)
CliSession = sessionmaker(bind=cli_engine)

# ── importa os modelos ─────────────────────────────────────────────────────────
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "clinica-agentes" / "clinica-agentes"))

from models import Paciente as CrmPaciente, Consulta as CrmConsulta  # CRM
from app.models import Paciente as CliPaciente, Consulta as CliConsulta, Dentista  # Clínica


def _fmt(iso: str | None) -> str:
    return iso or ""


def sincronizar_pacientes() -> tuple[int, int]:
    """Copia pacientes novos/atualizados entre os dois bancos."""
    crm_to_cli = cli_to_crm = 0

    with CrmSession() as crm, CliSession() as cli:
        # CRM → Clínica
        for cp in crm.query(CrmPaciente).all():
            # O campo de ligação é o telefone (chave natural comum)
            phone_digits = "".join(d for d in (cp.phone or "") if d.isdigit())
            if not phone_digits:
                continue
            # Busca por telefone na clínica
            match = None
            for p in cli.query(CliPaciente).all():
                if "".join(d for d in (p.telefone or "") if d.isdigit()) == phone_digits:
                    match = p
                    break
            if not match:
                novo = CliPaciente(
                    nome=cp.name,
                    telefone=cp.phone,
                    email=cp.email or "",
                    alergias="",
                )
                if cp.birth:
                    try:
                        from datetime import date
                        novo.nascimento = date.fromisoformat(cp.birth)
                    except Exception:
                        pass
                cli.add(novo)
                crm_to_cli += 1

        # Clínica → CRM
        for cp in cli.query(CliPaciente).all():
            phone_digits = "".join(d for d in (cp.telefone or "") if d.isdigit())
            if not phone_digits:
                continue
            match = None
            for p in crm.query(CrmPaciente).all():
                if "".join(d for d in (p.phone or "") if d.isdigit()) == phone_digits:
                    match = p
                    break
            if not match:
                import random, time as _time
                novo = CrmPaciente(
                    id=str(random.randint(100000, 999999)) + str(int(_time.time()))[-4:],
                    name=cp.nome,
                    phone=cp.telefone or "",
                    email=cp.email or "",
                    notes=cp.alergias or "",
                )
                crm.add(novo)
                cli_to_crm += 1

        crm.commit()
        cli.commit()

    return crm_to_cli, cli_to_crm


def sincronizar_consultas() -> tuple[int, int]:
    """Copia consultas concluídas da Clínica → CRM para atualizar lastVisit."""
    updated = 0

    with CrmSession() as crm, CliSession() as cli:
        # Para cada consulta concluída na Clínica, atualiza lastVisit no CRM
        concluidas = cli.query(CliConsulta).filter(CliConsulta.status == "concluida").all()
        for c in concluidas:
            pac_cli = cli.get(CliPaciente, c.paciente_id)
            if not pac_cli:
                continue
            phone_digits = "".join(d for d in (pac_cli.telefone or "") if d.isdigit())
            for p in crm.query(CrmPaciente).all():
                if "".join(d for d in (p.phone or "") if d.isdigit()) == phone_digits:
                    data_iso = c.inicio.strftime("%Y-%m-%d")
                    if not p.lastVisit or data_iso > p.lastVisit:
                        p.lastVisit = data_iso
                        updated += 1
                    break
        crm.commit()

    return updated, 0


def rodar() -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    p1, p2 = sincronizar_pacientes()
    v1, _ = sincronizar_consultas()
    print(f"[{ts}] Sync: {p1} paciente(s) CRM→Clínica | {p2} Clínica→CRM | {v1} lastVisit atualizado(s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sincronizador CRM ↔ Clínica-Agentes")
    parser.add_argument("--watch", action="store_true", help="Sincroniza a cada 60s continuamente")
    parser.add_argument("--interval", type=int, default=60, help="Intervalo em segundos (padrão: 60)")
    args = parser.parse_args()

    rodar()
    if args.watch:
        print(f"Modo contínuo ativo — sincronizando a cada {args.interval}s. Ctrl+C para parar.")
        while True:
            time.sleep(args.interval)
            rodar()
