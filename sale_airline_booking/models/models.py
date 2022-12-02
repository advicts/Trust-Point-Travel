# -*- coding: utf-8 -*-

from collections import defaultdict
from datetime import datetime
from dateutil.relativedelta import relativedelta
from odoo.tools import float_compare

from odoo import api, fields, models, SUPERUSER_ID, _
from odoo.addons.stock.models.stock_rule import ProcurementException
from odoo.tools import groupby

class ProductProduct(models.Model):
    _inherit = 'product.product'

    is_airline_ticket = fields.Boolean("Is Airline Ticket")

class StockRuleExt(models.Model):
    _inherit = 'stock.rule'

    @api.model
    def _run_buy(self, procurements):
        procurements_by_po_domain = defaultdict(list)
        errors = []
        for procurement, rule in procurements:

            # Get the schedule date in order to find a valid seller
            procurement_date_planned = fields.Datetime.from_string(procurement.values['date_planned'])

            supplier = False
            if procurement.values.get('supplierinfo_id'):
                supplier = procurement.values['supplierinfo_id']
            else:
                supplier = procurement.product_id.with_company(procurement.company_id.id)._select_seller(
                    partner_id=procurement.values.get("supplierinfo_name"),
                    quantity=procurement.product_qty,
                    date=procurement_date_planned.date(),
                    uom_id=procurement.product_uom)

            # Fall back on a supplier for which no price may be defined. Not ideal, but better than
            # blocking the user.
            supplier = supplier or procurement.product_id._prepare_sellers(False).filtered(
                lambda s: not s.company_id or s.company_id == procurement.company_id
            )[:1]

            try:
                sale_id = self.env["sale.order"].search([("name", "=", procurement.origin)])
                order_line =self.env["sale.order.line"].search(
                    [("order_id", "=", sale_id.id), ("product_uom_qty", "=", procurement.product_qty),
                     ("product_id", "=", procurement.product_id.id)], limit=1)
                vendor = order_line.supplier_id
            except Exception as error:
                vendor = False
            if vendor:
                partner = self.env['res.partner'].search([('id', '=', vendor.id)], limit=1)
                supplier = self.env['product.supplierinfo'].search([('partner_id', '=', partner.id)], limit=1)
                if not supplier:
                    supplier = self.env['product.supplierinfo'].create({
                        "partner_id":partner.id
                    })
            ## Added ths

            if not supplier:
                msg = _('There is no matching vendor price to generate the purchase order for product %s (no vendor defined, minimum quantity not reached, dates not valid, ...). Go on the product form and complete the list of vendors.') % (procurement.product_id.display_name)
                errors.append((procurement, msg))

            partner = supplier.partner_id
            # we put `supplier_info` in values for extensibility purposes
            procurement.values['supplier'] = supplier
            procurement.values['propagate_cancel'] = rule.propagate_cancel

            domain = rule._make_po_get_domain(procurement.company_id, procurement.values, partner)
            procurements_by_po_domain[domain].append((procurement, rule))

        if errors:
            raise ProcurementException(errors)

        for domain, procurements_rules in procurements_by_po_domain.items():
            # Get the procurements for the current domain.
            # Get the rules for the current domain. Their only use is to create
            # the PO if it does not exist.
            procurements, rules = zip(*procurements_rules)

            # Get the set of procurement origin for the current domain.
            origins = set([p.origin for p in procurements])
            # Check if a PO exists for the current domain.
            po = self.env['purchase.order'].sudo().search([dom for dom in domain], limit=1)
            company_id = procurements[0].company_id
            if not po:
                positive_values = [p.values for p in procurements if float_compare(p.product_qty, 0.0, precision_rounding=p.product_uom.rounding) >= 0]
                if positive_values:
                    # We need a rule to generate the PO. However the rule generated
                    # the same domain for PO and the _prepare_purchase_order method
                    # should only uses the common rules's fields.
                    vals = rules[0]._prepare_purchase_order(company_id, origins, positive_values)
                    # The company_id is the same for all procurements since
                    # _make_po_get_domain add the company in the domain.
                    # We use SUPERUSER_ID since we don't want the current user to be follower of the PO.
                    # Indeed, the current user may be a user without access to Purchase, or even be a portal user.
                    po = self.env['purchase.order'].with_company(company_id).with_user(SUPERUSER_ID).create(vals)
            else:
                # If a purchase order is found, adapt its `origin` field.
                if po.origin:
                    missing_origins = origins - set(po.origin.split(', '))
                    if missing_origins:
                        po.write({'origin': po.origin + ', ' + ', '.join(missing_origins)})
                else:
                    po.write({'origin': ', '.join(origins)})

            procurements_to_merge = self._get_procurements_to_merge(procurements)
            procurements = self._merge_procurements(procurements_to_merge)

            po_lines_by_product = {}
            grouped_po_lines = groupby(po.order_line.filtered(lambda l: not l.display_type and l.product_uom == l.product_id.uom_po_id), key=lambda l: l.product_id.id)
            for product, po_lines in grouped_po_lines:
                po_lines_by_product[product] = self.env['purchase.order.line'].concat(*po_lines)
            po_line_values = []

            for procurement in procurements:
                po_lines = po_lines_by_product.get(procurement.product_id.id, self.env['purchase.order.line'])
                po_line = po_lines._find_candidate(*procurement)

                if po_line:
                    # If the procurement can be merge in an existing line. Directly
                    # write the new values on it.
                    vals = self._update_purchase_order_line(procurement.product_id,
                        procurement.product_qty, procurement.product_uom, company_id,
                        procurement.values, po_line)
                    po_line.write(vals)
                else:
                    if float_compare(procurement.product_qty, 0, precision_rounding=procurement.product_uom.rounding) <= 0:
                        # If procurement contains negative quantity, don't create a new line that would contain negative qty
                        continue
                    # If it does not exist a PO line for current procurement.
                    # Generate the create values for it and add it to a list in
                    # order to create it in batch.
                    partner = procurement.values['supplier'].partner_id
                    po_line_values.append(self.env['purchase.order.line']._prepare_purchase_order_line_from_procurement(
                        procurement.product_id, procurement.product_qty,
                        procurement.product_uom, procurement.company_id,
                        procurement.values, po, order_line.net_total_supplier))

                    # Check if we need to advance the order date for the new line
                    order_date_planned = procurement.values['date_planned'] - relativedelta(
                        days=procurement.values['supplier'].delay)
                    if fields.Date.to_date(order_date_planned) < fields.Date.to_date(po.date_order):
                        po.date_order = order_date_planned
            self.env['purchase.order.line'].sudo().create(po_line_values)


class PurchaseOrderLine(models.Model):

    _inherit = 'purchase.order.line'

    @api.model
    def _prepare_purchase_order_line_from_procurement(self, product_id, product_qty, product_uom, company_id, values, po, price_unit=False):
        res = super(PurchaseOrderLine, self)._prepare_purchase_order_line_from_procurement(product_id, product_qty, product_uom, company_id, values, po)
        if price_unit:
            res['price_unit'] = price_unit
        return res


class ResUser(models.Model):

    _inherit = 'res.users'

    sale_commission_percent = fields.Float("Sales Commission %")

class AccountTax(models.Model):

    _inherit = 'account.tax'

    @api.model
    def _compute_taxes_for_single_line(self, base_line, handle_price_include=True, include_caba_tags=False, early_pay_discount_computation=None, early_pay_discount_percentage=None):
        to_update_vals, tax_values_list = super(AccountTax, self)._compute_taxes_for_single_line(base_line=base_line, handle_price_include=handle_price_include, include_caba_tags=include_caba_tags, early_pay_discount_computation=early_pay_discount_computation, early_pay_discount_percentage=early_pay_discount_percentage)
        to_update_vals['price_subtotal'] = base_line['price_subtotal']
        to_update_vals['price_total'] = base_line['price_subtotal']
        return to_update_vals, tax_values_list

class SaleOrder(models.Model):

    _inherit = "sale.order"

    @api.depends('order_line.tax_id', 'order_line.price_unit', 'amount_total', 'amount_untaxed')
    def _compute_tax_totals(self):
        for order in self:
            order_lines = order.order_line.filtered(lambda x: not x.display_type)
            order.tax_totals = self.env['account.tax']._prepare_tax_totals(
                [x._convert_to_tax_base_line_dict() for x in order_lines],
                order.currency_id,
            )

    @api.depends("order_line.uatp_amount", "order_line.net_total_supplier")
    def _compute_uatp_amount(self):
        for order in self:
            uatp_amount = float()
            net_total_supplier = float()
            for line in order.order_line:
                uatp_amount += line.uatp_amount
                net_total_supplier += line.net_total_supplier
            order.update({
                    'uatp_amount':uatp_amount,
                    'net_total_supplier':net_total_supplier,
                })

    uatp_amount = fields.Monetary("Total UATP Amount", compute="_compute_uatp_amount")
    net_total_supplier = fields.Monetary("Supplier Net Total", compute="_compute_uatp_amount")

    sale_commission_percent = fields.Float("Sale Commission Percent", related="user_id.sale_commission_percent")

    @api.depends("amount_total", "sale_commission_percent")
    def _compute_sale_commission_amount(self):
        for order in self:
            sale_commission_amount = float()
            if order.sale_commission_percent and order.amount_total:
                sale_commission_amount = (order.sale_commission_percent / 100) * order.amount_total

            order.update({
                    'sale_commission_amount':sale_commission_amount
                })

    sale_commission_amount = fields.Monetary("Sales Commission Amount", compute="_compute_sale_commission_amount")

class SaleOrderLine(models.Model):

    _inherit = "sale.order.line"

    @api.onchange("ticket_no", "pnr", "pax_name")
    def onchange_ticket_pax_pnr(self):
        name = str()
        if self.ticket_no:
            name += "{}/".format(self.ticket_no)
        if self.pnr:
            name += "{}/".format(self.pnr)
        if self.pax_name:
            name += "{}".format(self.pax_name)
        self.name = name

    maturity_period = fields.Integer("Maturity Period", default=0)
    supplier_id = fields.Many2one("res.partner", domain=False ,required=True)
    routing_id = fields.Many2one("sale.airline.routing")
    ticket_no = fields.Char("Ticket No.")
    pax_name = fields.Char("Pax Name")
    exchange_no = fields.Char("Exchange #")
    xo_no = fields.Char("XO No.")
    issue_date = fields.Date("Issue Date")
    flight_date = fields.Date("Flight Date")
    deal_code = fields.Char("Deal Code")
    gds = fields.Char("GDS")
    remark = fields.Text("Remark")
    is_airline_ticket = fields.Boolean("Is Airline Ticket", related="product_id.is_airline_ticket")

    tax_1 = fields.Float("Tax 1")
    tax_2 = fields.Float("Tax 2")
    tax_3 = fields.Float("Tax 3")
    tax_4 = fields.Float("Tax 4")

    fare_basis_id = fields.Many2one("sale.airline.fare.basis", "Fare Basis")
    class_id = fields.Many2one("sale.airline.class", "Class")

    @api.depends("net_total_supplier", "price_subtotal")
    def _compute_uatp_amount(self):
        """"""
        for line in self:
            line.uatp_amount = line.price_subtotal - line.net_total_supplier


    @api.depends("fare_tax", "cut_fare_supplier", "discount_amount_supplier")
    def _compute_net_total_supplier(self):
        """
        """
        for line in self:
            line.net_total_supplier = line.fare_tax - line.discount_amount_supplier - line.cut_fare_supplier

    cut_fare_customer = fields.Float("Customer Cut Fare")
    cut_fare_supplier = fields.Float("Supplier Cut Fare")
    service_charge = fields.Float("Service Charge %")

   
    net_total_supplier = fields.Float("Net Total Supplier", compute="_compute_net_total_supplier")
    uatp_amount = fields.Monetary("UATP Amount", compute="_compute_uatp_amount")

    @api.depends("discount", "price_total", "discount_supplier", "product_uom_qty", "price_unit")
    def _compute_discount(self):
        for line in self:
            gross_fare = line.price_unit * line.product_uom_qty
            line.discount_amount_supplier = gross_fare * ((line.discount_supplier) / 100) if line.discount_supplier else 0 
            line.discount_amount = gross_fare * ((line.discount or 1) / 100) if line.discount else 0

    discount_supplier = fields.Float("Supplier Discount %")
    discount_amount = fields.Monetary("Discount Amount", compute="_compute_discount")
    discount_amount_supplier = fields.Monetary("Supplier Discount Amount", compute="_compute_discount")

    pnr = fields.Char("PNR")
    flight_no = fields.Char("Flight No.")
    return_date = fields.Date("Return Date")

    @api.depends('product_uom_qty', 'discount', 'discount_amount', 'price_unit', 'tax_id', 'service_charge', 'service_charge_amount', 'cut_fare_customer', 'tax_1', 'tax_2','tax_3','tax_4')
    def _compute_amount(self):
        """
        Compute the amounts of the SO line.
        """
        for line in self:
            tax_results = self.env['account.tax']._compute_taxes([line._convert_to_tax_base_line_dict()])
            totals = list(tax_results['totals'].values())[0]
            amount_untaxed = totals['amount_untaxed'] + line.service_charge_amount - line.cut_fare_customer
            amount_tax = totals['amount_tax']
            custom_tax = line.tax_1 + line.tax_2 + line.tax_3 + line.tax_4
            fare_tax = (line.product_uom_qty * line.price_unit) + amount_tax + custom_tax

            service_charge_amount = float()
            if fare_tax and line.service_charge:
                service_charge_amount = (line.service_charge / 100) * fare_tax

            price_subtotal = fare_tax - line.discount_amount + service_charge_amount - line.cut_fare_customer
            line.update({
                'price_subtotal': price_subtotal,
                'price_tax': amount_tax,
                'service_charge_amount':service_charge_amount,
                'price_total': price_subtotal + amount_tax,
                'fare_tax':fare_tax,
            })
            if self.env.context.get('import_file', False) and not self.env.user.user_has_groups('account.group_account_manager'):
                line.tax_id.invalidate_recordset(['invoice_repartition_line_ids'])
    
    fare_tax = fields.Monetary("Fare + Taxes", compute="_compute_amount")
    service_charge_amount = fields.Float("Service Charge Amount", compute="_compute_amount")

    def _prepare_invoice_line(self, **optional_values):
        res = super(SaleOrderLine, self)._prepare_invoice_line(**optional_values)
        res['price_unit'] = self.price_subtotal
        return res

class AirlineRouting(models.Model):

    _name = 'sale.airline.routing'

    name = fields.Char("Routing", required=True)


class AirlineFareBasis(models.Model):

    _name = 'sale.airline.fare.basis'

    name = fields.Char("Fare Basis", required=True)

class AirlineClass(models.Model):

    _name = 'sale.airline.class'

    name = fields.Char("Class", required=True)