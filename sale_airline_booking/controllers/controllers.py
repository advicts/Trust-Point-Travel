# -*- coding: utf-8 -*-
# from odoo import http


# class SaleAirlineBooking(http.Controller):
#     @http.route('/sale_airline_booking/sale_airline_booking', auth='public')
#     def index(self, **kw):
#         return "Hello, world"

#     @http.route('/sale_airline_booking/sale_airline_booking/objects', auth='public')
#     def list(self, **kw):
#         return http.request.render('sale_airline_booking.listing', {
#             'root': '/sale_airline_booking/sale_airline_booking',
#             'objects': http.request.env['sale_airline_booking.sale_airline_booking'].search([]),
#         })

#     @http.route('/sale_airline_booking/sale_airline_booking/objects/<model("sale_airline_booking.sale_airline_booking"):obj>', auth='public')
#     def object(self, obj, **kw):
#         return http.request.render('sale_airline_booking.object', {
#             'object': obj
#         })
