# PJUD Civil - Mapa de Endpoints

## Autenticación

### Login
```
POST /sessionN.php
Content-Type: application/x-www-form-urlencoded

Params:
- 7f9d8a6356360386f79afd5691435626f470dee1: {jwt_token}  # Token de la página
- g-recaptcha-response-seg-clave_hn: {captcha_token}       # Token de reCAPTCHA v3
- rut: {rut_sin_dv}                                         # Ej: 16021492
- password: {password}

Response: 200 OK (establece cookies de sesión)
Session duration: ~25 minutos
```

### reCAPTCHA
- Site key: `6LelLWkUAAAAANPDMkBxllo_QJe5RQVpg6V2pIDt`
- Actions:
  - `validate_captcha_seg_clave_hn` - Login
  - `validate_captcha_detcau_civil` - Detalle de causa (Consulta Unificada)

---

## Consulta Unificada (búsqueda pública)

### Buscar Causas Civiles por ROL
```
POST /ADIR_871/civil/consultaRitCivil.php
Content-Type: application/x-www-form-urlencoded

Params:
- competencia: 3                    # 3 = Civil
- conCorte: 0                       # 0 = Todas, o código específico
- conTribunal: 0                    # 0 = Todos, o código específico
- conTipoBus: 1                     # 1 = Por RIT
- conTipoCausa: C                   # C, V, E, A, F, I
- conRolCausa: {numero_rol}         # Ej: 1234
- conEraCausa: {año}                # Ej: 2024
- conCaratulado: ""                 # Opcional

Response: HTML con tabla de resultados
```

#### Estructura de respuesta (HTML):
```html
<tr>
    <td align="center">
        <a onClick="detalleCausaCivil('{jwt_case_token}');">
            <i class="fa fa-search"></i>
        </a>
    </td>
    <td nowrap>{ROL}</td>          <!-- Ej: C-1234-2024 -->
    <td>{FECHA_INGRESO}</td>       <!-- Ej: 31/05/2024 -->
    <td>{CARATULADO}</td>          <!-- Ej: BANCO/DEMANDADO -->
    <td nowrap>{TRIBUNAL}</td>     <!-- Ej: 1º Juzgado Civil de Santiago -->
</tr>
```

### Detalle de Causa (Consulta Unificada)
```
POST /ADIR_871/civil/modal/causaCivil.php
Content-Type: application/x-www-form-urlencoded

Params:
- dtaCausa: {jwt_case_token}                    # Token de la búsqueda
- token: 917cfa057160fbb6de2eb86da2348e42       # Token fijo
- tokenCaptcha: CONTENEDORSII                    # O token de reCAPTCHA

Response: HTML con detalle completo de la causa
Status 405: Si falta contexto/referrer correcto
```

**NOTA:** Este endpoint requiere:
1. Estar en contexto de indexN.php, O
2. reCAPTCHA válido (action: `validate_captcha_detcau_civil`)

---

## Mis Causas (causas del abogado autenticado)

### Listar Causas Civiles del Abogado
```
POST /misCausas/civil/consultaMisCausasCivil.php
Content-Type: application/x-www-form-urlencoded

Params:
- rutMisCauCiv: {rut_sin_dv}        # Ej: 16021492
- dvMisCauCiv: {dv}                 # Ej: 9
- tipoMisCauCiv: 0                  # 0 = Todos, o C, V, E, A, F, I
- rolMisCauCiv: ""                  # Filtro por rol (opcional)
- anhoMisCauCiv: ""                 # Filtro por año (opcional)
- tipCausaMisCauCiv: M              # M = Mis causas
- estadoCausaMisCauCiv: 1           # 1 = Activas, 0 = Todas
- nombreMisCauCiv: ""               # Filtro
- apePatMisCauCiv: ""               # Filtro
- apeMatMisCauCiv: ""               # Filtro

Response: HTML con filas de causas
```

#### Columnas de respuesta:
1. Acciones (ícono ver detalle)
2. RIT (Ej: C-1234-2024)
3. Tribunal
4. Caratulado
5. Fecha Ingreso
6. Estado Cuaderno
7. Cuaderno
8. Institución

### Detalle de Causa (Mis Causas)
```
POST /misCausas/civil/modal/misCausasCivil.php
Content-Type: application/x-www-form-urlencoded

Params:
- dtaCausa: {jwt_case_token}
- token: df32271e9cdca2704ff289941058a253    # Token fijo (diferente al de Consulta Unificada)

Response: HTML con modal de detalle + movimientos
```

---

## JWT Token de Caso

Los tokens de caso son JWT firmados con HS256.

### Estructura:
```json
{
  "iss": "https://oficinajudicialvirtual.pjud.cl",
  "aud": "https://oficinajudicialvirtual.pjud.cl",
  "iat": 1780678338,
  "exp": 1780680138,
  "data": "<encrypted_case_data>"
}
```

### Propiedades:
- **Validez:** 30 minutos desde emisión
- **Datos:** Encriptados en campo `data`
- **Uso:** Identificar la causa para obtener detalle

---

## Tokens Fijos Conocidos

| Contexto | Token |
|----------|-------|
| Consulta Unificada - Detalle | `917cfa057160fbb6de2eb86da2348e42` |
| Mis Causas - Detalle | `df32271e9cdca2704ff289941058a253` |
| SII Container | `CONTENEDORSII` |

---

## Flujo de Scraping Recomendado

### Para "Mis Causas" (causas del abogado):
1. Login con captcha
2. POST a `/misCausas/civil/consultaMisCausasCivil.php`
3. Parsear HTML para extraer tokens de cada causa
4. Para cada causa, POST a `/misCausas/civil/modal/misCausasCivil.php`
5. Parsear movimientos del modal

### Para "Consulta Unificada" (búsqueda pública):
1. Login con captcha
2. POST a `/ADIR_871/civil/consultaRitCivil.php`
3. Parsear HTML para extraer tokens
4. Para detalle: necesita contexto de página o reCAPTCHA adicional

---

## Competencias (Códigos)

| Código | Competencia |
|--------|-------------|
| 1 | Suprema |
| 2 | Apelaciones |
| 3 | Civil |
| 4 | Laboral |
| 5 | Penal |
| 6 | Cobranza |
| 7 | Familia |

---

## Tablas HTML por Competencia (Mis Causas)

| ID Tabla | Competencia |
|----------|-------------|
| `dtaTableDetalleMisCauSup` | Suprema |
| `dtaTableDetalleMisCauApe` | Apelaciones |
| `dtaTableDetalleMisCauCiv` | Civil |
| `dtaTableDetalleMisCauLab` | Laboral |
| `dtaTableDetalleMisCauPen` | Penal |
| `dtaTableDetalleMisCauCob` | Cobranza |
| `dtaTableDetalleMisCauFam` | Familia |
