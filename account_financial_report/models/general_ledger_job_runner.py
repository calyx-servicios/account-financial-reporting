from odoo import models, fields, http, api
from odoo.http import request
from datetime import datetime, date, time
from pathlib import Path
import time as gettime
import logging
import os
import shutil
import pytz
import logging
_logger = logging.getLogger(__name__)

tz = pytz.timezone('America/Argentina/Buenos_Aires')
_logger = logging.getLogger(__name__)

tag = "GENERAL LEDGER CRON: "
first_call = True

def get_param(env, param):
    return env['ir.config_parameter'].sudo().get_param(param, False)

def format_time(seconds):
    ms = int((seconds % 1) * 1000)
    seconds = int(seconds)

    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h > 0:
        return f"{h}h {m}m {s}s {ms}ms"
    elif m > 0:
        return f"{m}m {s}s {ms}ms"
    else:
        return f"{s}s {ms}ms"

def format_size(file_path):
    file_path = Path(file_path)
    size = file_path.stat().st_size
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024

def get_reports_dir():
    module_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    ledger_path = os.path.join(module_path, 'ledger_reports')
    os.makedirs(ledger_path, exist_ok=True)
    return ledger_path

def prepare_company(ledger_path, company):
    company_dir_path = company.name
    company_path = os.path.abspath(os.path.join(ledger_path, company_dir_path))
    os.makedirs(company_path, exist_ok=True)
    today = datetime.now(tz).strftime('%Y-%m-%d')
    today_path = os.path.abspath(os.path.join(company_path, today))
    os.makedirs(today_path, exist_ok=True)
    for name in os.listdir(company_path):
        if name != today:
            rm_path = os.path.abspath(os.path.join(company_path, name))
            shutil.rmtree(rm_path)
    ledger_path = os.path.abspath(os.path.join(today_path, "general_ledger.xlsx"))
    generate_ledger = not os.path.isfile(ledger_path)
    return generate_ledger, ledger_path


class GeneralLedgerJobRunner(models.Model):
    _name = "general.ledger.job.runner"
    _description = "General Ledger Job Runner"

    name = fields.Char(
        string="Fecha",
        compute="compute_fields"
    )
    date = fields.Date()
    disk_space = fields.Char(
        string="Espacio en Disco",
        compute="compute_fields"
    )
    exec_time = fields.Char(
        string="Tiempo de procesado",
        compute="compute_fields"
    )
    file_path = fields.Text()
    # Doble cc intencional
    ccompany_id = fields.Many2one(
        string="Compañia",
        comodel_name="res.company"
    )

    def compute_fields(self):
        for rec in self:
            end_date = rec.date.strftime('%d-%m-%Y')
            start_date = rec.date.replace(day=1).strftime('%d-%m-%Y')
            rec.name = f"Desde: {start_date} - Hasta: {end_date}"
            rec.disk_space = format_size(rec.file_path)
            rec.exec_time = "-"
            if rec.file_path:
                time_path = os.path.abspath(os.path.join(os.path.dirname(rec.file_path), "time.txt"))
                if os.path.isfile(time_path):
                    with open(time_path, "r") as f:
                        rec.exec_time = f.read().strip()

    def action_open_ledger_reports(self):
        self.create_records()
        return {
            "type": "ir.actions.act_window",
            "name": "Archivos libro mayor",
            "res_model": "general.ledger.job.runner",
            "view_mode": "tree",
        }

    def create_records(self):
        today = datetime.now(tz).date()
        self = self.sudo()
        self.search([
            ("date", "<", today)
        ]).unlink()
        exist_records = self.search([
            ("date", "=", today)
        ])
        for rec in exist_records:
            if not os.path.isfile(rec.file_path):
                rec.unlink()
        exist_records = self.search([
            ("date", "=", today)
        ])
        exist_companys = exist_records.mapped("ccompany_id.id")
        today = datetime.now(tz).strftime('%Y-%m-%d')
        reports_path = get_reports_dir()
        for company in self.env["res.company"].search([]):
            if company.id not in exist_companys:
                company_path = os.path.abspath(os.path.join(reports_path, company.name))
                today_path = os.path.abspath(os.path.join(company_path, today))
                ledger_path = os.path.abspath(os.path.join(today_path, "general_ledger.xlsx"))
                file_exists = os.path.isfile(ledger_path)
                if file_exists:
                    self.create({
                        "date": today,
                        "ccompany_id": company.id,
                        "file_path": ledger_path
                    })

    def cron_enqueue_jobs(self):
        jobs_domain = [
            ("identity_key", "=", "general_ledger_unique_job"),
            ("state", "in", ["pending", "enqueued", "started"]),
        ]
        global first_call
        if first_call:
            jobs = self.env["queue.job"].search(jobs_domain)
            jobs.unlink()
            first_call = False
        hour = datetime.now(tz).hour
        if not (get_param(self.env, "general_ledger_cron.anytime") == "True"):
            if not (0 <= hour <= 6):
                _logger.info(tag + "no ejecuta - fuera de horario")
                return
        _logger.info(tag + "Encolando job queue_job")
        existing = self.env["queue.job"].search(jobs_domain, limit=1)
        if existing:
            _logger.info(
                tag + "Job no encolado porque ya existe uno activo (id=%s, state=%s)",
                existing.id,
                existing.state
            )
            return
        self.env["general.ledger.job.runner"].with_delay(
            channel="root.general_ledger",
            identity_key="general_ledger_unique_job"
        ).run_general_ledger()

    def run_general_ledger(self):
        global first_call
        first_call = False
        ledger_reports_path = get_reports_dir()
        companys = self.env["res.company"].search([])
        for company in companys:
            generate_company, ledger_path = prepare_company(ledger_reports_path, company)
            if generate_company:
                _logger.info(tag + "Generando Reporte para compañia %s" % company.name)
                self.generate_ledger(ledger_path, company)
                break

    def generate_ledger(self, ledger_path, company):
        wizard = self.env["general.ledger.report.wizard"].create({})
        wizard.company_id = company.id
        today = datetime.now(tz).date()
        first_day = today.replace(day=1)
        wizard.date_from = first_day
        wizard.date_to = today
        test_date_from = get_param(self.env, "general_ledger_cron.test_date_from")
        test_date_to = get_param(self.env, "general_ledger_cron.test_date_to")
        if test_date_from:
            wizard.date_from = datetime.strptime(test_date_from, "%Y-%m-%d").date()
        if test_date_to:
            wizard.date_to = datetime.strptime(test_date_to, "%Y-%m-%d").date()
        data = wizard._prepare_report_data()
        start_time = gettime.perf_counter()
        report_name = "a_f_r.report_general_ledger_xlsx"
        report_type = "xlsx"
        report = self.env["ir.actions.report"].search(
            [("report_name", "=", report_name), ("report_type", "=", report_type)],
            limit=1,
        )
        content, content_type = report._render_xlsx(report.report_name, wizard.ids, data=data)
        end_time = gettime.perf_counter()
        elapsed = end_time - start_time
        formated_time = format_time(elapsed)

        with open(ledger_path, "wb") as f:
            f.write(content)
        time_path = os.path.abspath(os.path.join(os.path.dirname(ledger_path), "time.txt"))
        with open(time_path, "w") as f:
            f.write(formated_time)
        _logger.info(tag + "Reporte generado %s" % company.name)

    def download_report(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'/ledger/download/{self.id}',
            'target': 'self',
        }

class LedgerDownloadController(http.Controller):

    @http.route('/ledger/download/<int:record_id>', type='http', auth='user')
    def download_ledger(self, record_id, **kwargs):
        record = request.env['general.ledger.job.runner'].browse(record_id)

        file_path = record.file_path

        if not file_path or not os.path.exists(file_path):
            return request.not_found()

        rec_date = record.date.strftime('%d-%m-%Y')
        rec_company = record.ccompany_id.name
        filename = f"Libro Mayor {rec_company} {rec_date}.xlsx"
        with open(file_path, 'rb') as f:
            file_content = f.read()
        headers = [
            ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('Content-Disposition', f'attachment; filename="{filename}"')
        ]

        return request.make_response(file_content, headers)
