from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel

class RepuestoFacturado(BaseModel):
    nombre_repuesto: str
    cantidad: int
    precio_unitario_historico: float
    subtotal_linea: float

class FacturaDetalladaResponse(BaseModel):
    id: int
    fecha_emision: datetime
    subtotal_repuestos: float
    costo_mano_obra: float
    iva: float
    total_pagar: float
    placa: str
    modelo: Optional[str] = None
    mecanico: Optional[str] = None
    repuestos: List[RepuestoFacturado]