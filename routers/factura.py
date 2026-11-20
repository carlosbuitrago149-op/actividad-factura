from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import joinedload
from core.database import obtener_bd
from models.factura import Factura as FacturaModel
from models.orden import Orden as OrdenModel
from models.repuesto import Repuesto as RepuestoModel
from models.orden_repuesto import OrdenRepuesto as OrdenRepuestoModel
from schemas.factura import FacturaDetalladaResponse, RepuestoFacturado

router = APIRouter(prefix="/facturas", tags=["Facturación"])


@router.get("/{orden_id}", response_model=FacturaDetalladaResponse)
async def obtener_factura(orden_id: int, db: AsyncSession = Depends(obtener_bd)):
    res = await db.execute(select(FacturaModel).where(FacturaModel.orden_id == orden_id))
    factura = res.scalar_one_or_none()
    if not factura:
        raise HTTPException(status_code=404, detail="Esta orden no tiene factura")

    res_orden = await db.execute(
        select(OrdenModel).options(joinedload(OrdenModel.mecanico)).where(OrdenModel.id == orden_id)
    )
    orden = res_orden.scalar_one()

    res_rep = await db.execute(
        select(RepuestoModel.nombre, OrdenRepuestoModel.cantidad, OrdenRepuestoModel.precio_unitario_historico)
        .join(RepuestoModel, RepuestoModel.id == OrdenRepuestoModel.repuesto_id)
        .where(OrdenRepuestoModel.orden_id == orden_id)
    )

    repuestos = [
        RepuestoFacturado(
            nombre_repuesto=nombre,
            cantidad=cantidad,
            precio_unitario_historico=precio,
            subtotal_linea=round(cantidad * precio, 2),
        )
        for nombre, cantidad, precio in res_rep.all()
    ]

    return FacturaDetalladaResponse(
        id=factura.id,
        fecha_emision=factura.fecha_emision,
        subtotal_repuestos=factura.subtotal_repuestos,
        costo_mano_obra=factura.costo_mano_obra,
        iva=factura.iva,
        total_pagar=factura.total_pagar,
        placa=orden.placa,
        modelo=orden.modelo,
        mecanico=orden.mecanico.nombre_completo if orden.mecanico else None,
        repuestos=repuestos,
    )