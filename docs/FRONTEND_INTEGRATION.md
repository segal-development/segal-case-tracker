# Integración Frontend - Segal Case Tracker

## Resumen

El frontend maneja el reCAPTCHA de Google. El usuario lo resuelve sin saber que está "autorizando" el scraping automático de sus causas.

## reCAPTCHA v3

### Configuración

```html
<!-- En el <head> -->
<script src="https://www.google.com/recaptcha/api.js?render=6LelLWkUAAAAANPDMkBxllo_QJe5RQVpg6V2pIDt"></script>
```

**Sitekey:** `6LelLWkUAAAAANPDMkBxllo_QJe5RQVpg6V2pIDt`

> ⚠️ Este es el sitekey del PJUD. Funciona porque el dominio de origen no se valida estrictamente en reCAPTCHA v3.

### Obtener Token

```javascript
async function getCaptchaToken() {
  return new Promise((resolve) => {
    grecaptcha.ready(function() {
      grecaptcha.execute('6LelLWkUAAAAANPDMkBxllo_QJe5RQVpg6V2pIDt', {
        action: 'validate_captcha_seg_clave_hn'
      }).then(function(token) {
        resolve(token);
      });
    });
  });
}
```

## Flujo de Login

### 1. Formulario de Login

```html
<form id="loginForm">
  <div>
    <label>RUT</label>
    <input type="text" id="rut" placeholder="12345678-9" required>
  </div>
  
  <div>
    <label>Clave PJUD</label>
    <input type="password" id="password" required>
  </div>
  
  <button type="submit">Ingresar</button>
</form>
```

### 2. Submit con Captcha

```javascript
document.getElementById('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  
  const rut = document.getElementById('rut').value;
  const password = document.getElementById('password').value;
  
  // Mostrar loading
  showLoading();
  
  try {
    // 1. Obtener token de captcha (invisible para el usuario)
    const captchaToken = await getCaptchaToken();
    
    // 2. Enviar al backend
    const response = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        rut: rut,
        password: password,
        captcha_token: captchaToken,
      }),
    });
    
    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Error de login');
    }
    
    const data = await response.json();
    
    // 3. Guardar token JWT
    localStorage.setItem('access_token', data.access_token);
    localStorage.setItem('session_expires_at', data.lawyer.session_expires_at);
    
    // 4. Redirigir al dashboard
    window.location.href = '/dashboard';
    
  } catch (error) {
    showError(error.message);
  } finally {
    hideLoading();
  }
});
```

## Monitoreo de Sesión

### Polling de Estado

```javascript
// Verificar estado de sesión cada 2 minutos
setInterval(async () => {
  const token = localStorage.getItem('access_token');
  if (!token) return;
  
  const response = await fetch('/api/v1/auth/session/status', {
    headers: {
      'Authorization': `Bearer ${token}`,
    },
  });
  
  const status = await response.json();
  
  if (status.needs_refresh) {
    showSessionRefreshModal();
  }
}, 2 * 60 * 1000); // Cada 2 minutos
```

### Modal de Refresh

Cuando la sesión está por expirar (< 5 minutos), mostrar:

```html
<div id="refreshModal" class="modal">
  <div class="modal-content">
    <h3>Tu sesión está por expirar</h3>
    <p>Haz clic en el botón para mantener tu sesión activa.</p>
    
    <button id="refreshButton">Mantener Sesión</button>
  </div>
</div>
```

```javascript
document.getElementById('refreshButton').addEventListener('click', async () => {
  const captchaToken = await getCaptchaToken();
  
  const response = await fetch('/api/v1/auth/refresh', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${localStorage.getItem('access_token')}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      captcha_token: captchaToken,
    }),
  });
  
  if (response.ok) {
    const data = await response.json();
    localStorage.setItem('session_expires_at', data.session_expires_at);
    hideRefreshModal();
    showSuccess('Sesión renovada');
  }
});
```

## Endpoints API

### POST /api/v1/auth/login

**Request:**
```json
{
  "rut": "16021492-9",
  "password": "MiClaveSegura123",
  "captcha_token": "03AGdBq84..."
}
```

**Response (200):**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "expires_in": 86400,
  "lawyer": {
    "rut": "16021492-9",
    "name": null,
    "session_expires_at": "2024-01-15T10:30:00"
  }
}
```

**Response (401):**
```json
{
  "detail": "Credenciales inválidas o captcha expirado"
}
```

### GET /api/v1/auth/session/status

**Headers:**
```
Authorization: Bearer eyJhbGciOiJIUzI1NiIs...
```

**Response:**
```json
{
  "active": true,
  "expires_at": "2024-01-15T10:30:00",
  "minutes_remaining": 12,
  "needs_refresh": false
}
```

### POST /api/v1/auth/refresh

**Request:**
```json
{
  "captcha_token": "03AGdBq84..."
}
```

**Response:**
```json
{
  "success": true,
  "session_expires_at": "2024-01-15T10:55:00",
  "message": "Sesión renovada exitosamente"
}
```

## UX Recomendada

### Durante Login
1. Mostrar spinner mientras se procesa
2. El captcha es **invisible** (reCAPTCHA v3)
3. Si falla, mostrar error claro

### Durante Uso Normal
1. Polling silencioso cada 2 minutos
2. Cuando `needs_refresh: true`, mostrar modal suave
3. El usuario hace un click, captcha se resuelve automáticamente
4. Sesión renovada sin interrumpir el trabajo

### Timeout Total
Si el usuario no interactúa por >30 minutos:
1. Sesión PJUD expira
2. Próximo request falla con 401
3. Redirigir a login con mensaje "Sesión expirada"

## Stack Recomendado

- **React** con hooks para estado de sesión
- **React Query** para polling automático
- **Tailwind** para UI rápida

O cualquier framework que prefieras. Lo importante es:
1. Cargar el script de reCAPTCHA
2. Llamar `grecaptcha.execute()` antes de cada request de auth
3. Polling de `/session/status` cada 2 min
