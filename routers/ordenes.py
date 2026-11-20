from fastapi import APIRouter, HTTPException, Query, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from core.database import obtener_bd
from models.orden import Orden as OrdenModel
from models.mecanico import Mecanico as MecanicoModel
from models.repuesto import Repuesto as RepuestoModel
from models.orden_repuesto import OrdenRepuesto as OrdenRepuestoModel
from models.factura import Factura as FacturaModel
from typing import Optional, List
from schemas.ordenes import OrdenCreate, OrdenPatch, OrdenResponse, AgregarRepuestoOrden, RepuestoEnOrdenResponse
from dependencias import verificar_api_key, parametros_paginacion
from sqlalchemy.orm import joinedload

router = APIRouter(prefix="/ordenes", tags=["Gestionar ordenes"])

TASA_IVA = 0.19


async def orden_tiene_factura(db: AsyncSession, orden_id: int) -> bool:
    res = await db.execute(select(FacturaModel.id).where(FacturaModel.orden_id == orden_id))
    return res.first() is not None


@router.get("/", response_model=List[OrdenResponse])
async def obtener_ordenes(
    estado: Optional[str] = None,
    paginacion: dict = Depends(parametros_paginacion),
    db: AsyncSession = Depends(obtener_bd)
):
    sentencia = select(OrdenModel).options(joinedload(OrdenModel.mecanico))
    if estado:
        sentencia = sentencia.where(OrdenModel.estado == estado)
    sentencia = sentencia.offset(paginacion["skip"]).limit(paginacion["limit"])
    resultado = await db.execute(sentencia)
    return resultado.scalars().all()


@router.get("/{orden_id}", response_model=OrdenResponse)
async def obtener_orden_por_id(
    orden_id: int,
    db: AsyncSession = Depends(obtener_bd)
):
    sentencia = select(OrdenModel).options(joinedload(OrdenModel.mecanico)).where(OrdenModel.id == orden_id)
    resultado = await db.execute(sentencia)

    orden = resultado.scalar_one_or_none()
    if not orden:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    return orden


@router.post("/", status_code=201)
async def crear_orden(
    orden: OrdenCreate,
    api_key: str = Depends(verificar_api_key),
    db: AsyncSession = Depends(obtener_bd)
):
    consulta_mec = select(MecanicoModel).where(MecanicoModel.id == orden.mecanico_id)
    res_mec = await db.execute(consulta_mec)
    mecanico_db = res_mec.scalar_one_or_none()

    if not mecanico_db or not mecanico_db.activo:
        raise HTTPException(
            status_code=400,
            detail=f"El mecanico con el ID {orden.mecanico_id} no existe o está inactivo."
        )

    nueva_orden = OrdenModel(**orden.model_dump())
    db.add(nueva_orden)

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Error al guardar la base de datos")

    await db.refresh(nueva_orden)

    return {"mensaje": "Orden creada", "orden": nueva_orden}


@router.delete("/{placa}", status_code=204)
async def eliminar_orden(
    placa: str,
    db: AsyncSession = Depends(obtener_bd)
):
    sentencia = select(OrdenModel).where(OrdenModel.placa == placa)
    resultado = await db.execute(sentencia)
    orden = resultado.scalar_one_or_none()

    if not orden:
        raise HTTPException(status_code=404, detail="La orden no existe")

    await db.delete(orden)
    await db.commit()
    return None


@router.patch("/{orden_id}")
async def actualizacion_parcial(
    orden_id: int,
    orden_patch: OrdenPatch,
    db: AsyncSession = Depends(obtener_bd)
):
    res = await db.execute(select(OrdenModel).where(OrdenModel.id == orden_id))
    orden = res.scalar_one_or_none()

    if not orden:
        raise HTTPException(status_code=404, detail="La orden no existe")

    datos = orden_patch.model_dump(exclude_unset=True)
    mano_obra = datos.pop("mano_obra", None) or 0.0

    if "nuevo_estado" in datos:
        datos["estado"] = (datos.pop("nuevo_estado") or "").strip().capitalize()

    if datos.get("estado") == "Reparada":
        # CANDADO 1: idempotencia (no duplicar factura ni volver a sumar mano de obra)
        if await orden_tiene_factura(db, orden_id):
            return {"mensaje": "La orden ya estaba facturada, no se generó otra factura", "orden": orden}

        for campo, valor in datos.items():
            setattr(orden, campo, valor)
        orden.costo_reparacion = (orden.costo_reparacion or 0.0) + mano_obra

        # CANDADO 2: costo mayor a cero
        if orden.costo_reparacion <= 0:
            await db.rollback()
            raise HTTPException(
                status_code=400,
                detail="No se puede marcar como Reparada ni facturar una orden con costo 0."
            )

        # CANDADO 4: cálculo tributario
        res_sub = await db.execute(
            select(func.coalesce(
                func.sum(OrdenRepuestoModel.cantidad * OrdenRepuestoModel.precio_unitario_historico), 0.0
            )).where(OrdenRepuestoModel.orden_id == orden_id)
        )
        subtotal_repuestos = float(res_sub.scalar_one())

        total_neto = orden.costo_reparacion
        iva = round(total_neto * TASA_IVA, 2)

        db.add(FacturaModel(
            orden_id=orden_id,
            subtotal_repuestos=round(subtotal_repuestos, 2),
            costo_mano_obra=round(total_neto - subtotal_repuestos, 2),
            iva=iva,
            total_pagar=round(total_neto + iva, 2),
        ))

        try:
            await db.commit()
        except IntegrityError:
            # Dos PATCH simultáneos: unique=True en facturas.orden_id evita el duplicado
            await db.rollback()
            raise HTTPException(status_code=409, detail="La factura de esta orden ya fue generada.")
        except Exception:
            await db.rollback()
            raise HTTPException(status_code=500, detail="Error generando la factura.")

        await db.refresh(orden)
        return {"mensaje": "Orden reparada y factura generada", "orden": orden}

    # Cualquier otro cambio (sin facturar)
    for campo, valor in datos.items():
        setattr(orden, campo, valor)
    if mano_obra:
        orden.costo_reparacion = (orden.costo_reparacion or 0.0) + mano_obra

    try:
        await db.commit()
        await db.refresh(orden)
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Error aplicando la actualización parcial.")

    return {"mensaje": "La orden ha sido actualizada con éxito", "orden": orden}


@router.post("/{orden_id}/repuestos", status_code=201, response_model=RepuestoEnOrdenResponse)
async def agregar_repuesto_a_orden(
    orden_id: int,
    datos: AgregarRepuestoOrden,
    db: AsyncSession = Depends(obtener_bd)
):
    # 1. Validar que la orden exista
    res_orden = await db.execute(select(OrdenModel).where(OrdenModel.id == orden_id))
    orden_db = res_orden.scalar_one_or_none()
    if not orden_db:
        raise HTTPException(status_code=404, detail="La orden de servicio no existe")

    # CANDADO 3: congelamiento histórico
    if orden_db.estado == "Reparada" or await orden_tiene_factura(db, orden_id):
        raise HTTPException(
            status_code=400,
            detail="No se pueden modificar repuestos de una orden ya cerrada/facturada"
        )

    # 2. Validar que el repuesto exista
    res_rep = await db.execute(select(RepuestoModel).where(RepuestoModel.id == datos.repuesto_id))
    rep_db = res_rep.scalar_one_or_none()
    if not rep_db:
        raise HTTPException(status_code=404, detail="El repuesto no existe")

    # 3. Validar stock
    if rep_db.stock_actual < datos.cantidad:
        raise HTTPException(
            status_code=400,
            detail=f"Stock insuficiente. En almacen tengo: {rep_db.stock_actual} y se solicitaron: {datos.cantidad}"
        )

    # Operacion transaccional
    rep_db.stock_actual -= datos.cantidad
    subtotal = rep_db.precio_venta * datos.cantidad
    orden_db.costo_reparacion += subtotal

    detalle = OrdenRepuestoModel(
        orden_id=orden_db.id,
        repuesto_id=rep_db.id,
        cantidad=datos.cantidad,
        precio_unitario_historico=rep_db.precio_venta
    )

    db.add(detalle)

    try:
        await db.commit()
        await db.refresh(detalle)
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Error agregando el repuesto a la orden.")

    return detalle