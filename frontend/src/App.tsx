import { QueryClientProvider } from '@tanstack/react-query';
import { createMemoryRouter, Navigate, RouterProvider, type RouteObject } from 'react-router';
import { createQueryClient } from './api/queryClient';
import { StartParamContext } from './lib/startParamContext';
import { parseStartParam, startParamToPath } from './lib/startParam';
import { getStartParamRaw } from './lib/telegram';
import { AdminHub } from './screens/AdminHub/AdminHub';
import { AdminInvite } from './screens/AdminInvite/AdminInvite';
import { ApplicationWizard } from './screens/ApplicationWizard/ApplicationWizard';
import { CreateEvent } from './screens/CreateEvent/CreateEvent';
import { RouteError } from './screens/ErrorScreen/RouteError';
import { EventSettings } from './screens/EventSettings/EventSettings';
import { Home } from './screens/Home/Home';
import { Invite } from './screens/Invite/Invite';
import { Join } from './screens/Join/Join';
import { MyTrip } from './screens/MyTrip/MyTrip';
import { Profile } from './screens/Profile/Profile';
import { TransferDetails } from './screens/TransferDetails/TransferDetails';
import { TripForm } from './screens/TripForm/TripForm';
import { Trips } from './screens/Trips/Trips';

const queryClient = createQueryClient();

const startParam = parseStartParam(getStartParamRaw());

export const routes: RouteObject[] = [
  {
    // Корневой маршрут без пути: исключение в любом экране — ErrorScreen с «Мои группы».
    errorElement: <RouteError />,
    children: [
      { path: '/', element: <Home /> },
      { path: '/profile', element: <Profile /> },
      { path: '/join', element: <Join /> },
      { path: '/invite/:code', element: <Invite /> },
      // `/events/new` — до `/events/:id` (для читаемости; роутер и так ранжирует статический сегмент выше).
      { path: '/events/new', element: <CreateEvent /> },
      { path: '/events/:id', element: <MyTrip /> },
      // Мастер заявки: шаги — внутреннее состояние экрана, а не записи истории.
      { path: '/events/:id/apply', element: <ApplicationWizard /> },
      { path: '/events/:id/admin', element: <AdminHub /> },
      { path: '/events/:id/admin/settings', element: <EventSettings /> },
      { path: '/events/:id/admin/invite', element: <AdminInvite /> },
      { path: '/events/:id/admin/trips', element: <Trips /> },
      { path: '/events/:id/admin/trips/new', element: <TripForm /> },
      { path: '/events/:id/admin/trips/:tripId', element: <TripForm /> },
      { path: '/events/:id/admin/transfers/:tid', element: <TransferDetails /> },
      // Неизвестный путь (в т.ч. deep link на ещё не готовый экран) — на «Мои группы».
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
];

// Memory router: URL внутри Telegram-webview не важен. Первый экран — по start_param.
const router = createMemoryRouter(routes, { initialEntries: [startParamToPath(startParam)] });

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <StartParamContext.Provider value={startParam}>
        {/* Телефон запрашивает бот после /start; на вступление без него бэкенд отвечает PHONE_REQUIRED. */}
        <RouterProvider router={router} />
      </StartParamContext.Provider>
    </QueryClientProvider>
  );
}
