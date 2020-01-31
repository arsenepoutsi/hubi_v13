# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, exceptions, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_round
from odoo.addons import decimal_precision as dp


class StockMove(models.Model):
    _inherit = 'stock.move'

    bom_line_id = fields.Many2one('mrp.bom.line', 'BoM Line')
    unit_factor = fields.Float('Unit Factor')
    is_done = fields.Boolean(
        'Done', compute='_compute_is_done',
        store=True,
        help='Technical Field to order moves')
    needs_lots = fields.Boolean('Tracking', compute='_compute_needs_lots')
    order_finished_lot_ids = fields.Many2many('stock.production.lot', compute='_compute_order_finished_lot_ids')
    finished_lots_exist = fields.Boolean('Finished Lots Exist', compute='_compute_order_finished_lot_ids')

    @api.depends('active_move_line_ids.qty_done', 'active_move_line_ids.product_uom_id')
    def _compute_done_quantity(self):
        super(StockMove, self)._compute_done_quantity()

    @api.depends('product_id.tracking')
    def _compute_needs_lots(self):
        for move in self:
            move.needs_lots = move.product_id.tracking != 'none'

    @api.depends('state')
    def _compute_is_done(self):
        for move in self:
            move.is_done = (move.state in ('done', 'cancel'))

    def _action_confirm(self, merge=True):
        moves = self.env['stock.move']
        for move in self:
            moves |= move.action_explode()
        # we go further with the list of ids potentially changed by action_explode
        return super(StockMove, moves)._action_confirm(merge=merge)

    def action_explode(self):

        """ Explodes pickings """
        ## in order to explode a move, we must have a picking_type_id on that move because otherwise the move
        ## won't be assigned to a picking and it would be weird to explode a move into several if they aren't
        ## all grouped in the same picking.
        if not self.picking_type_id:
            return self
        bom = self.env['mrp.bom'].sudo()._bom_find(product=self.product_id, company_id=self.company_id.id)
        if not bom or bom.type != 'phantom':
            return self
        phantom_moves = self.env['stock.move']
        processed_moves = self.env['stock.move']
        factor = self.product_uom._compute_quantity(self.product_uom_qty, bom.product_uom_id) / bom.product_qty
        # boms, lines = bom.sudo().explode(self.product_id, factor, picking_type=bom.picking_type_id)
        boms, lines = bom.sudo().explode(self.product_id, factor, picking_type=None)
        for bom_line, line_data in lines:
            phantom_moves += self._generate_move_phantom(bom_line, line_data['qty'])

        for new_move in phantom_moves:
            processed_moves |= new_move.action_explode()

        if processed_moves and self.state == 'assigned':
            # Set the state of resulting moves according to 'assigned' as the original move is assigned
            processed_moves.write({'state': 'assigned'})
        ## delete the move with original product which is not relevant anymore
        self.sudo().unlink()
        return processed_moves

    def _prepare_phantom_move_values(self, bom_line, quantity):
        return {
            'picking_id': self.picking_id.id if self.picking_id else False,
            'product_id': bom_line.product_id.id,
            'product_uom': bom_line.product_uom_id.id,
            'product_uom_qty': quantity,
            'state': 'draft',  # will be confirmed below
            'name': self.name,
        }

    def _generate_move_phantom(self, bom_line, quantity):
        if bom_line.product_id.type in ['product', 'consu']:
            return self.copy(default=self._prepare_phantom_move_values(bom_line, quantity))
        return self.env['stock.move']

    def _generate_consumed_move_line(self, qty_to_add, final_lot, lot=False):
        if lot:
            move_lines = self.move_line_ids.filtered(lambda ml: ml.lot_id == lot and not ml.lot_produced_id)
        else:
            move_lines = self.move_line_ids.filtered(lambda ml: not ml.lot_id and not ml.lot_produced_id)

        # Sanity check: if the product is a serial number and `lot` is already present in the other
        # consumed move lines, raise.
        if lot and self.product_id.tracking == 'serial' and lot in self.move_line_ids.filtered(
                lambda ml: ml.qty_done).mapped('lot_id'):
            raise UserError(
                _('You cannot consume the same serial number twice. Please correct the serial numbers encoded.'))

        for ml in move_lines:
            rounding = ml.product_uom_id.rounding
            if float_compare(qty_to_add, 0, precision_rounding=rounding) <= 0:
                break
            quantity_to_process = min(qty_to_add, ml.product_uom_qty - ml.qty_done)
            qty_to_add -= quantity_to_process

            new_quantity_done = (ml.qty_done + quantity_to_process)
            if float_compare(new_quantity_done, ml.product_uom_qty, precision_rounding=rounding) >= 0:
                ml.write({'qty_done': new_quantity_done, 'lot_produced_id': final_lot.id})
            else:
                new_qty_reserved = ml.product_uom_qty - new_quantity_done
                default = {'product_uom_qty': new_quantity_done,
                           'qty_done': new_quantity_done,
                           'lot_produced_id': final_lot.id}
                ml.copy(default=default)
                ml.with_context(bypass_reservation_update=True).write(
                    {'product_uom_qty': new_qty_reserved, 'qty_done': 0})

        if float_compare(qty_to_add, 0, precision_rounding=self.product_uom.rounding) > 0:
            # Search for a sub-location where the product is available. This might not be perfectly
            # correct if the quantity available is spread in several sub-locations, but at least
            # we should be closer to the reality. Anyway, no reservation is made, so it is still
            # possible to change it afterwards.
            quants = self.env['stock.quant']._gather(self.product_id, self.location_id, lot_id=lot, strict=False)
            available_quantity = self.product_id.uom_id._compute_quantity(
                self.env['stock.quant']._get_available_quantity(
                    self.product_id, self.location_id, lot_id=lot, strict=False
                ), self.product_uom
            )
            location_id = False
            if float_compare(qty_to_add, available_quantity, precision_rounding=self.product_uom.rounding) < 0:
                location_id = quants.filtered(lambda r: r.quantity > 0)[-1:].location_id

            vals = {
                'move_id': self.id,
                'product_id': self.product_id.id,
                'location_id': location_id.id if location_id else self.location_id.id,
                'location_dest_id': self.location_dest_id.id,
                'product_uom_qty': 0,
                'product_uom_id': self.product_uom.id,
                'qty_done': qty_to_add,
                'lot_produced_id': final_lot.id,
            }
            if lot:
                vals.update({'lot_id': lot.id})
            self.env['stock.move.line'].create(vals)



