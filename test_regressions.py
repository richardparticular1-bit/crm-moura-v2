import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import main
import models as m
from backup_service import GROUPS, export_records, include_files, restore_records


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        @event.listens_for(self.engine, "connect")
        def enforce_fks(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        m.Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine)
        self.who = SimpleNamespace(id=1, clinicaId=1, isSuperAdmin=True, isClinicaAdmin=True)
        self.patches = [patch.object(main, "SessionLocal", self.sessions),
                        patch.object(main, "_usuario_logado", lambda _: self.who)]
        for p in self.patches:
            p.start()
        with self.sessions() as db:
            db.add_all([m.Clinica(id=1, nome="A"), m.Clinica(id=2, nome="B")])
            db.flush()
            db.add_all([m.Paciente(id="p1", clinicaId=1, name="Teste"), m.Paciente(id="p2", clinicaId=2, name="Outra clínica"),
                        m.Profissional(id=1, clinicaId=1, nome="Dentista"), m.Profissional(id=2, clinicaId=2, nome="Outro")])
            db.flush()
            db.add_all([m.Consulta(id="a1", clinicaId=1, patientId="p1", profissionalId=1, date="2026-10-04", time="10:00"),
                        m.Lancamento(id="l1", clinicaId=1, patientId="p1", valor=10000, vencimento="2026-10-04"),
                        m.Tarefa(id=1, clinicaId=1, titulo="Teste", patientId="p1")])
            db.commit()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.engine.dispose()

    def error(self, status, fn, *args):
        with self.assertRaises(HTTPException) as caught:
            fn(*args)
        self.assertEqual(caught.exception.status_code, status)

    def test_appointment_cannot_change_to_other_clinic_patient(self):
        self.error(404, main.update_appointment, "a1", main.ConsultaIn(patientId="p2", date="2026-10-04", time="11:00"), None)
        with self.sessions() as db:
            self.assertEqual(db.get(m.Consulta, "a1").patientId, "p1")

    def test_task_cannot_change_to_other_clinic_patient(self):
        self.error(404, main.update_tarefa, 1, main.TarefaIn(titulo="Teste", patientId="p2"), None)

    def test_financial_update_cannot_change_clinic(self):
        self.error(404, main.update_lancamento, "l1", main.LancamentoIn(patientId="p2", valor=10000, vencimento="2026-10-04"), None)

    def test_zero_and_negative_payment_leave_balance_unchanged(self):
        for amount in (0, -1):
            self.error(422, main.receber_lancamento, "l1", main.ReceberIn(valorRecebido=amount), None)
        with self.sessions() as db:
            self.assertIsNone(db.get(m.Lancamento, "l1").pagoEm)
            self.assertEqual(db.get(m.Lancamento, "l1").valor, 10000)

    def test_full_and_partial_payment(self):
        r = main.receber_lancamento("l1", main.ReceberIn(valorRecebido=4000, restanteVencimento="2026-11-04"), None)
        with self.sessions() as db:
            self.assertEqual(db.get(m.Lancamento, r["restanteId"]).valor, 6000)
        main.receber_lancamento(r["restanteId"], main.ReceberIn(), None)

    def test_team_member_cannot_create_or_update_accounts(self):
        self.who.isSuperAdmin = self.who.isClinicaAdmin = False
        data = main.UsuarioIn(nome="Teste", login="teste", senha="12345678")
        self.error(403, main.create_usuario, data, None)
        self.error(403, main.update_usuario, 1, data, None)

    def test_linked_professional_cannot_be_deleted(self):
        self.error(409, main.delete_profissional, 1, None)

    def test_patient_with_history_cannot_be_deleted(self):
        self.error(409, main.delete_patient, "p1", None)

    def test_clinic_with_feedback_deletes_in_fk_order(self):
        with self.sessions() as db:
            db.get(m.Clinica, 2).ativa = False
            user = m.Usuario(clinicaId=2, nome="Equipe", username="equipe", senhaHash="teste")
            db.add(user)
            db.flush()
            db.add(m.FeedbackSAC(id="feedback", clinicaId=2, usuarioId=user.id, categoria="outro", titulo="Teste", mensagem="Teste"))
            db.commit()
        with patch.object(main, "_dump_data", return_value={}), patch.object(main, "_apagar_storage_da_clinica"):
            main.excluir_clinica(2, None)
        with self.sessions() as db:
            self.assertIsNone(db.get(m.Clinica, 2))

    def test_last_admin_cannot_be_deactivated(self):
        with self.sessions() as db:
            admin = m.Usuario(clinicaId=1, nome="Admin", username="admin", senhaHash="teste", isClinicaAdmin=True)
            db.add(admin)
            db.commit()
            uid = admin.id
        self.error(409, main.update_usuario, uid, main.UsuarioIn(nome="Admin", login="admin", ativo=False), None)

    def test_unsafe_client_id_is_rejected(self):
        with self.assertRaises(ValueError):
            main.PacienteIn(id="' onclick='alert(1)", name="Teste")

    def backup(self):
        with self.sessions() as db:
            return include_files(export_records(db, 1), lambda p: (b"imagem", "image/png"))

    def test_backup_roundtrip_remaps_relationships(self):
        data = self.backup()
        with self.sessions() as db:
            result = restore_records(db, 2, data, lambda *a: None, lambda *a: None)
            self.assertEqual(result["patients"], 1)
            imported = db.query(m.Consulta).filter_by(clinicaId=2).one()
            self.assertNotEqual(imported.patientId, "p1")
            self.assertEqual(db.get(m.Paciente, imported.patientId).clinicaId, 2)
            self.assertNotEqual(imported.profissionalId, 1)
            self.assertEqual(db.get(m.Profissional, imported.profissionalId).clinicaId, 2)
            self.error(409, restore_records, db, 2, data, lambda *a: None, lambda *a: None)

    def test_every_clinical_group_roundtrips(self):
        from sqlalchemy import DateTime, Integer
        from datetime import datetime
        with self.sessions() as db:
            for index, (name, model) in enumerate(GROUPS):
                if db.query(model).filter_by(clinicaId=1).count():
                    continue
                values = {"clinicaId": 1}
                for column in model.__table__.columns:
                    if column.name == "clinicaId" or column.nullable or column.default is not None:
                        continue
                    if column.primary_key and isinstance(column.type, Integer):
                        continue
                    references = {"patientId": "p1", "appointmentId": "a1", "profissionalId": 1,
                                  "orcamentoId": db.query(m.Orcamento.id).filter_by(clinicaId=1).scalar(),
                                  "modeloId": db.query(m.AnamneseModelo.id).filter_by(clinicaId=1).scalar()}
                    if column.name in references:
                        value = references[column.name]
                        values[column.name] = value[0] if isinstance(value, tuple) else value
                    elif isinstance(column.type, DateTime):
                        values[column.name] = datetime.now()
                    elif isinstance(column.type, Integer):
                        values[column.name] = 1
                    else:
                        values[column.name] = f"v{index}"
                db.add(model(**values))
                db.flush()
            db.commit()
        data = self.backup()
        with self.sessions() as db:
            restore_records(db, 2, data, lambda *a: None, lambda *a: None)
            for name, model in GROUPS:
                count = db.query(model).filter_by(clinicaId=2).count()
                # A clínica destino já tinha um paciente e um profissional.
                self.assertEqual(count, len(data[name]) + (1 if name in ("patients", "profissionais") else 0), name)

    def test_invalid_reference_rolls_back_every_record(self):
        data = self.backup()
        data["appointments"][0]["patientId"] = "p2"
        with self.sessions() as db:
            self.error(422, restore_records, db, 2, data, lambda *a: None, lambda *a: None)
            self.assertEqual(db.query(m.Paciente).filter_by(clinicaId=2).count(), 1)
            self.assertEqual(db.query(m.Profissional).filter_by(clinicaId=2).count(), 1)

    def test_old_or_incomplete_backup_is_rejected(self):
        with self.sessions() as db:
            self.error(422, restore_records, db, 2, {"patients": []}, lambda *a: None, lambda *a: None)
            data = self.backup()
            del data["odontograma_marcas"]
            self.error(422, restore_records, db, 2, data, lambda *a: None, lambda *a: None)

    def test_files_restored_under_new_paths(self):
        with self.sessions() as db:
            db.get(m.Paciente, "p1").fotoPath = "_fotos/1/p1.jpg"
            db.commit()
        data = self.backup()
        calls = []
        with self.sessions() as db:
            restore_records(db, 2, data, lambda *args: calls.append(args), lambda _: None)
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0][0].startswith("clinicas/2/imports/"))
            self.assertEqual(calls[0][1], b"imagem")

    def test_storage_failure_rolls_back_and_cleans_uploaded_files(self):
        with self.sessions() as db:
            db.get(m.Paciente, "p1").fotoPath = "_fotos/1/p1.jpg"
            db.commit()
        data = self.backup()
        deleted = []
        def fail(*_):
            raise RuntimeError("Storage indisponível")
        with self.sessions() as db:
            self.error(422, restore_records, db, 2, data, fail, deleted.append)
            self.assertEqual(db.query(m.Paciente).filter_by(clinicaId=2).count(), 1)
            self.assertEqual(len(deleted), 1)


if __name__ == "__main__":
    unittest.main()
