from odoo import models, fields, http, api
from odoo.tools import date_utils
from odoo.http import request
from datetime import datetime, date, time
from pathlib import Path
import time as gettime
import logging
import os
import shutil
import pytz
import logging
import zipfile
import gc
_logger = logging.getLogger(__name__)

tz = pytz.timezone('America/Argentina/Buenos_Aires')
_logger = logging.getLogger(__name__)

tag = "GENERAL LEDGER CRON: "
first_call = True

def dividir_rango(fecha_fin_dia, n=5):
    tam_bloque = n
    total_dias = fecha_fin_dia
    partes = []

    inicio = 1

    while inicio <= total_dias:
        fin = inicio + tam_bloque - 1

        if fin >= total_dias:
            # último bloque
            partes.append((inicio, total_dias))
            break

        partes.append((inicio, fin))
        inicio = fin + 1

    # 🔹 Si el último bloque tiene 1 solo día → lo fusionamos
    if len(partes) >= 2:
        ultimo_inicio, ultimo_fin = partes[-1]
        if ultimo_fin - ultimo_inicio == 0:  # tamaño 1
            ante_inicio, _ = partes[-2]
            partes[-2] = (ante_inicio, ultimo_fin)
            partes.pop()

    return partes

def get_param(env, param):
    return env['ir.config_parameter'].sudo().get_param(param, False)

def get_time_path(file_path):
    file_path_dir = os.path.abspath(os.path.dirname(file_path))
    time_paths = list(Path(file_path_dir).rglob("time.txt"))
    time = 0
    for time_path in time_paths:
        with open(time_path, "r") as f:
            time += float(f.read().strip())
    if time:
        return format_time(time)
    else:
        return '-'

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
    extension_file = fields.Char(
        string="Formato"
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
            rec.extension_file = "-"
            if rec.file_path:
                if rec.file_path.endswith('.xlsx'):
                    rec.extension_file = "XLSX"
                if rec.file_path.endswith('.zip'):
                    rec.extension_file = "ZIP"
                rec.exec_time = get_time_path(rec.file_path)

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
                if not file_exists:
                    ledger_path = os.path.abspath(os.path.join(today_path, "general_ledger.zip"))
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
        try:
            global first_call
            first_call = False
            ledger_reports_path = get_reports_dir()
            companys = self.env["res.company"].search([])
            for company in companys:
                generate_company, ledger_path = prepare_company(ledger_reports_path, company)
                if generate_company and not company.divide_ledger_files:
                    _logger.info(tag + "Generando Reporte para compañia %s" % company.name)
                    self.generate_ledger(ledger_path, company)
                    break
                elif company.divide_ledger_files:
                    date_range, output_ledger_path = self.get_generate_company_by_part(company, ledger_path)
                    if date_range:
                        _logger.info(tag + "Generando Reporte para compañia %s desde %s hasta %s" % (company.name, date_range["date_from"], date_range["date_to"]))
                        self.generate_ledger(output_ledger_path, company, date_range=date_range)
                        break
                    if not date_range:
                        generated = self.generate_zip_ledger(company, ledger_path)
                        if generated:
                            break
        except Exception as e:
            _logger.exception(f"{tag} Error generando reporte de libro mayor")
        self.env.cr.commit()
        self.env.clear()
        gc.collect()

    def generate_zip_ledger(self, company, ledger_path):
        company_dir = os.path.abspath(os.path.dirname(ledger_path))
        zip_path = os.path.abspath(os.path.join(company_dir, "general_ledger.zip"))
        if not os.path.isfile(zip_path):
            _logger.info(tag + "Generando .ZIP para compañia %s" % company.name)
            ledger_paths = list(Path(company_dir).rglob("*.xlsx"))
            with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                for file_path in ledger_paths:
                    part_dir = os.path.basename(os.path.dirname(file_path))
                    if "part" in part_dir:
                        arcname = os.path.basename(file_path)
                        zf.write(file_path, arcname)
            return True
        return False

    def get_account_ledger_name(self, account):
        account_name = account.name.replace('/', '|')
        return f"Libro mayor {account_name}.xlsx"

    def get_generate_company_by_part(self, company, ledger_path):
        company_dir = os.path.abspath(os.path.dirname(ledger_path))
        today = datetime.today()
        test_date_to = get_param(self.env, "general_ledger_cron.test_date_to")
        if test_date_to:
            today = datetime.strptime(test_date_to, "%Y-%m-%d").date()
        end_day = int(today.day)
        rangos = dividir_rango(end_day)
        for i, date_range in enumerate(rangos):
            part = f"part{i+1}"
            part_dir = os.path.join(company_dir, part)
            os.makedirs(part_dir, exist_ok=True)
            date_from = today.replace(day=date_range[0])
            date_to = today.replace(day=date_range[1])
            file_name = "Libro mayor %s hasta %s.xlsx" % (
                date_from.strftime("%d-%m-%Y"),
                date_to.strftime("%d-%m-%Y")
            )
            part_ledger_path = os.path.abspath(os.path.join(part_dir, file_name))
            if not os.path.isfile(part_ledger_path):
                date_range_out = {
                    "date_from": date_from,
                    "date_to": date_to,
                }
                return date_range_out, part_ledger_path
        return False, False


    def _get_general_ledger_data(self):
        return {
            'date_from': False,
            'date_to': False,
            'only_posted_moves': True,
            'hide_account_at_0': False,
            'foreign_currency': True,
            'company_id': False,
            'account_ids': [],
            'partner_ids': [],
            'grouped_by': 'partners',
            'cost_center_ids': [],
            'show_cost_center': True,
            'journal_ids': [],
            'centralize': True,
            'fy_start_date': False,
            'unaffected_earnings_account': False,
            'account_financial_report_lang': 'en_US',
            'domain': []
        }

    def generate_ledger(self, ledger_path, company, date_range=False):
        self = self.with_context(create_xlsx_report=ledger_path)
        data = self._get_general_ledger_data()
        company_id = company.id
        today = datetime.now(tz).date()
        first_day = today.replace(day=1)
        date_from = first_day
        date_to = today
        test_date_to = get_param(self.env, "general_ledger_cron.test_date_to")
        if test_date_to:
            date_to = datetime.strptime(test_date_to, "%Y-%m-%d").date()
            date_from = date_to.replace(day=1)
        if date_range:
            date_from = date_range["date_from"]
            date_to = date_range["date_to"]
        data["date_from"] = date_from
        data["date_to"] = date_to
        data["company_id"] = company_id
        fy_start_date, foo = date_utils.get_fiscal_year(
            date_from,
            day=company.fiscalyear_last_day,
            month=int(company.fiscalyear_last_month),
        )
        data["fy_start_date"] = fy_start_date
        unaffected_earnings_account = self.env["account.account"].search([
            ("account_type", "=", "equity_unaffected"),
            ("company_id", "=", company.id),
        ], limit=1)
        data["unaffected_earnings_account"] = unaffected_earnings_account.id
        data["limit_text"] = self.env["general.ledger.report.wizard"]._limit_text
        start_time = gettime.perf_counter()
        report_name = "a_f_r.report_general_ledger_xlsx"
        report_type = "xlsx"
        report = self.env["ir.actions.report"].search(
            [("report_name", "=", report_name), ("report_type", "=", report_type)],
            limit=1,
        )
        report._render_xlsx(report.report_name, False, data=data)
        end_time = gettime.perf_counter()
        elapsed = end_time - start_time
        time_path = os.path.abspath(os.path.join(os.path.dirname(ledger_path), "time.txt"))
        with open(time_path, "w") as f:
            f.write(str(elapsed))
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
        if file_path.endswith(".zip"):
            filename = f"Libro Mayor {rec_company} {rec_date}.zip"
        else:
            filename = f"Libro Mayor {rec_company} {rec_date}.xlsx"
        with open(file_path, 'rb') as f:
            file_content = f.read()
        headers = [
            ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('Content-Disposition', f'attachment; filename="{filename}"')
        ]

        return request.make_response(file_content, headers)

class ResCompany(models.Model):
    _inherit="res.company"

    divide_ledger_files = fields.Boolean(
        String="Generar el reporte de libro mayor separado por partes"
    )
