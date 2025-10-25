# Noticiero automático (Python + GitHub Actions)

## Qué hace
- Recolecta titulares y contenido de las fuentes definidas.
- Filtra por palabras clave y por “reciente” (p. ej., últimas 24 h).
- Genera un resumen HTML y lo envía por correo.
- Se ejecuta automáticamente (cron) o bajo demanda desde **Actions**.

---

## Uso para no técnicos (2 pasos)

### 1) Editar la configuración
Abrir `config.yaml` → botón ✏️ → modificar → **Commit changes**.

Ejemplo:
```yaml
keywords:
  - "oro"
  - "pies"

tzname: "Europe/Madrid"
hours_recent: 24

email:
  user: "anartz2001@gmail.com"   # remitente
  to_emails:
    - "anartz2001@gmail.com"     # destinatarios

sources:
  - name: "MARCA"
    url: "https://www.marca.com/"
    max_to_fetch: 400
  - name: "EXPANSION"
    url: "https://www.expansion.com/"
    max_to_fetch: 400
  - name: "MUNDO_DEPORTIVO"
    url: "https://www.mundodeportivo.com/"
    max_to_fetch: 400
Qué pueden cambiar:

keywords: una o varias palabras. Dejar vacío para sin filtro.

hours_recent: horas de ventana (p. ej., 24).

email.user y email.to_emails: remitente y destinatarios.

sources: añadir/quitar medios. Solo una url por medio.

2) Ejecutar o esperar a la automatización
Automático: el flujo corre con la frecuencia configurada (por defecto cada 5 minutos).

Manual: pestaña Actions → Noticias automáticas → Run workflow.

Primera vez: configurar el correo
En la cuenta Gmail del remitente, activar Verificación en dos pasos.

Crear App Password (16 caracteres) para “Correo”.

En GitHub: Settings → Secrets and variables → Actions → New repository secret

Name: SMTP_PASS

Value: la App Password (sin espacios).

El script usa SMTP_USER desde config.yaml y SMTP_PASS desde el Secret.

Frecuencia de ejecución
El workflow está en .github/workflows/noticias.yml.

Frecuencias comunes (editar cron):

Cada 5 minutos: */5 * * * *

Cada hora: 0 * * * *

Diario 06:00 UTC (~07:00 Madrid en invierno): 0 6 * * *

Guardar el cambio con Commit changes.

Ver logs y resolver problemas
Pestaña Actions → abrir la última ejecución → ver pasos y salida.

Errores típicos:

SMTPAuthenticationError 535: Secret SMTP_PASS incorrecto o revocado.

“0 artículos”: ampliar hours_recent, revisar keywords, o añadir más sources.

Cambios en la web: si una fuente cambia su HTML, avísanos para ajustar el parser.

Seguridad y control
La contraseña de Gmail no está en el código. Está en Secrets.

Todo queda versionado. Si algo falla tras editar config.yaml, usar History para revertir.
