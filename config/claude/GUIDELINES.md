# Frontend Guidelines: Modular React Architecture

## 1. Directory Structure (Feature-First)
Wir spiegeln die Backend-Module im Frontend. Jedes Modul ist eine in sich geschlossene Einheit.



### Module Layout
`src/modules/[module-name]/`
- `components/`: Private Komponenten, die nur in diesem Modul genutzt werden.
- `hooks/`: Modulspezifische Business-Logik (State & Logic).
- `services/`: API-Calls (Axios/Fetch Wrapper) für dieses Modul.
- `types/`: TypeScript-Definitionen für dieses Modul.
- `index.ts`: Public API des Moduls (Exponiert nur, was andere Module brauchen).

---

## 2. Component Design (Layered Frontend)
Jede Komponente folgt dem Prinzip der **Separation of Concerns**:

| Layer | Responsibility | Pattern |
| :--- | :--- | :--- |
| **View (TSX)** | Rein visuelle Darstellung & Layout | Tailwind CSS Klassen. |
| **Logic (Hooks)** | Zustandsverwaltung & Events | Custom Hooks (`use[ModuleName]`). |
| **Data (Services)** | API-Kommunikation & Transformation | Async Functions / TanStack Query. |

### Rules
- **Functional Components ONLY**: Keine Class Components.
- **Props**: Immer über TypeScript Interfaces definieren.
- **Size**: Komponenten > 200 Zeilen müssen in Sub-Komponenten zerlegt werden.
- **No Inline Logic**: Komplexe Berechnungen oder `useEffect`-Orgien gehören in einen Custom Hook.

---

## 3. State & Data Fetching
- **Server State**: Nutze TanStack Query (React Query) für API-Calls. Es übernimmt Caching und Revalidierung automatisch.
- **Global State**: Minimalistisch halten. Nutze `Context API` für Themes/Auth oder `Zustand` für komplexere globale UI-States.
- **Local State**: `useState` / `useReducer` innerhalb von Custom Hooks.

---

## 4. Styling & UI (Tailwind CSS)
- **Utility-First**: Nutze Tailwind Klassen direkt im TSX.
- **Abstraction**: Erstelle nur dann eigene CSS-Klassen (`@apply`), wenn eine Utility-Kombination mehr als 5-mal identisch vorkommt.
- **Design System**: Nutze die `tailwind.config.js` für Farben, Fonts und Spacing, um Konsistenz zu garantieren.

---

## 5. Quality & Performance
- **Memoization**: Nutze `useMemo` und `useCallback` nur dort, wo teure Berechnungen oder unnötige Re-Renders von Child-Komponenten verhindert werden müssen.
- **Strict Mode**: Der `frontend-developer` Agent muss sicherstellen, dass keine "Unsafe Life-Cycles" genutzt werden.
- **Naming**: 
  - Komponenten: `PascalCase` (z.B. `UserProfile.tsx`)
  - Hooks: `camelCase` mit `use`-Prefix (z.B. `useUserSearch.ts`)
  - Services: `camelCase` (z.B. `authService.ts`)