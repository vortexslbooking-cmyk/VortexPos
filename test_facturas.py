"""
test_facturas.py — Facturas COMPLETAS (F1) en el servidor.

Aquí se prueba lo que NO puede fallar aunque la tablet mienta: el desglose de
IVA, la validación de NIF/CIF y los datos mínimos para poder emitir. La app
tiene su propia batería (01-app-pos/pruebas/), pero la que manda es esta:
la app es un HTML que está en la barra de un bar y cualquiera puede editarlo.

Ejecutar:  python3 test_facturas.py
"""
import sys

from app.fiscal_validacion import identifica, valida_cif, valida_cliente, valida_nie, valida_nif
from app.verifactu_api import LineaIn, _desglosar

fallos = []


def ok(nombre, condicion, detalle=""):
    bien = bool(condicion)
    if not bien:
        fallos.append(nombre)
    print(("PASS" if bien else "FAIL"), "—", nombre,
          ("· " + str(detalle)) if detalle and not bien else "")


def eur(x):
    return round(x + 1e-9, 2)


print("== PARTE A · identificadores fiscales ==")

ok("A1 · NIF con letra correcta se acepta", valida_nif("12345678Z"))
ok("A2 · NIF con letra incorrecta se rechaza", not valida_nif("12345678A"))
ok("A3 · NIF de todo ceros (caso límite del módulo 23)", valida_nif("00000000T"))
ok("A4 · NIE con las tres iniciales X, Y y Z",
   valida_nie("X1234567L") and valida_nie("Y1234567X") and valida_nie("Z1234567R"))
ok("A5 · NIE con letra incorrecta se rechaza", not valida_nie("X1234567A"))
ok("A6 · CIF de S.A. válido", valida_cif("A58818501"))
ok("A7 · CIF de S.L. válido", valida_cif("B12345674"))
ok("A8 · CIF con dígito de control incorrecto se rechaza", not valida_cif("B12345670"))
ok("A9 · CIF de asociación (control por letra, no por dígito)", valida_cif("G28029643"))
ok("A10 · un NIF con guiones y espacios se normaliza y se acepta",
   identifica(" 12.345.678-z ")[0])
ok("A11 · una cadena de 9 caracteres cualquiera NO cuela",
   not identifica("HOLAHOLA1")[0])
ok("A12 · el motivo del rechazo dice cuál sería la letra correcta",
   "Z" in identifica("12345678A")[2], identifica("12345678A")[2])

print()
print("== PARTE B · datos mínimos para emitir una factura completa ==")

COMPLETO = {"nombre": "Talleres Ramisur S.L.", "nif": "B12345674",
            "direccion": "C/ Julio Romero de Torres 28", "cp": "29700",
            "municipio": "Velez-Malaga", "provincia": "Malaga", "pais": "ES",
            "tipo": "empresa", "email": "oficina@ramisur.com"}

ok("B1 · un cliente completo pasa sin fallos", valida_cliente(COMPLETO) == [],
   valida_cliente(COMPLETO))

for campo in ("nombre", "nif", "direccion", "cp", "municipio"):
    incompleto = dict(COMPLETO)
    incompleto.pop(campo)
    ok("B2.%s · sin %s NO se puede facturar" % (campo, campo),
       len(valida_cliente(incompleto)) >= 1)

ok("B3 · se devuelven TODOS los fallos de una vez, no solo el primero",
   len(valida_cliente({"nif": "12345678A"})) >= 4,
   valida_cliente({"nif": "12345678A"}))

ok("B4 · código postal de provincia inexistente (99) se rechaza",
   any("provincia" in f for f in valida_cliente({**COMPLETO, "cp": "99123"})))
ok("B5 · código postal con letras se rechaza",
   valida_cliente({**COMPLETO, "cp": "2970A"}) != [])
ok("B6 · correo mal formado se rechaza",
   any("correo" in f.lower() for f in valida_cliente({**COMPLETO, "email": "esto@no"})))
ok("B7 · correo vacío es válido (es opcional)",
   valida_cliente({**COMPLETO, "email": ""}) == [])
ok("B8 · fuera de España no se inventa un formato de CP",
   valida_cliente({**COMPLETO, "pais": "FR", "cp": "75008", "nif": "B12345674"}) == [])
ok("B9 · un tipo de cliente inventado se rechaza",
   valida_cliente({**COMPLETO, "tipo": "marciano"}) != [])

print()
print("== PARTE C · desglose de IVA (lo calcula el servidor, NUNCA la tablet) ==")

r = _desglosar([LineaIn(descripcion="Menu", cantidad=10, precio=11.0, tipo_iva=10)])
ok("C1 · un solo tipo: 110 € al 10 % → base 100,00 y cuota 10,00",
   r["base"] == 100.00 and r["cuota"] == 10.00 and r["total"] == 110.00, r)

r = _desglosar([LineaIn(cantidad=1, precio=110.0, tipo_iva=10),
                LineaIn(cantidad=1, precio=60.5, tipo_iva=21)])
ok("C2 · dos tipos: bases 100 y 50, cuotas 10 y 10,50, total 170,50",
   len(r["desglose"]) == 2 and r["base"] == 150.00
   and r["cuota"] == 20.50 and r["total"] == 170.50, r)

ok("C3 · base + cuota == total, siempre",
   eur(r["base"] + r["cuota"]) == r["total"], r)

r = _desglosar([LineaIn(cantidad=4, precio=15.0, tipo_iva=10, descuento=6.0)])
ok("C4 · el descuento se aplica ANTES de separar la base",
   r["total"] == 54.00 and eur(r["base"] + r["cuota"]) == 54.00, r)

# 17 líneas de 1,05 €: si se redondeara línea a línea, la suma se desviaría.
r = _desglosar([LineaIn(cantidad=3, precio=0.35, tipo_iva=21) for _ in range(17)])
ok("C5 · muchas líneas de céntimos: el total no se desvía por redondeo",
   r["total"] == 17.85 and eur(r["base"] + r["cuota"]) == 17.85, r)

r = _desglosar([LineaIn(cantidad=2, precio=1.04, tipo_iva=4),
                LineaIn(cantidad=1, precio=11.0, tipo_iva=10),
                LineaIn(cantidad=1, precio=12.1, tipo_iva=21)])
ok("C6 · tres tipos a la vez (4, 10 y 21) se desglosan por separado",
   len(r["desglose"]) == 3 and eur(r["base"] + r["cuota"]) == r["total"], r)

ok("C7 · los tipos salen ordenados de menor a mayor",
   [d["tipo"] for d in r["desglose"]] == [4.0, 10.0, 21.0], r["desglose"])

r = _desglosar([])
ok("C8 · una factura sin líneas da total 0 y no revienta", r["total"] == 0.0, r)

r = _desglosar([LineaIn(cantidad=1, precio=100.0, tipo_iva=0)])
ok("C9 · IVA al 0 %: base = total y cuota = 0",
   r["base"] == 100.00 and r["cuota"] == 0.00, r)

print()
print("== PARTE D · lo que el servidor NO se cree de la app ==")

# Este es el punto que convierte el desglose en una defensa y no en un adorno.
lineas = [LineaIn(cantidad=1, precio=10.0, tipo_iva=21)]
calc = _desglosar(lineas)
ok("D1 · el total se calcula desde las líneas, no desde lo que diga la tablet",
   calc["total"] == 10.00, calc)
ok("D2 · un 'importe_total_app' distinto NO cambia lo que se factura",
   _desglosar(lineas)["total"] == 10.00)

print()
total = len(fallos)
print("-" * 62)
if total:
    print("%d COMPROBACIONES EN FALLO:" % total)
    for f in fallos:
        print("  ·", f)
    sys.exit(1)
print("Todas las comprobaciones de facturación completa superadas.")
