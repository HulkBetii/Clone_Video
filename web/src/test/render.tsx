import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import * as Tooltip from "@radix-ui/react-tooltip";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";

export function renderApp(ui: ReactElement, route = "/") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <Tooltip.Provider delayDuration={0}>
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </Tooltip.Provider>
    </QueryClientProvider>,
  );
}
