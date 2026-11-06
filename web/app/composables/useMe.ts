export interface Me { email: string; role: 'viewer' | 'admin'; tenant: string }

/** The signed in user, or null. Filled by the route middleware and by the sign in page. */
export const useMe = () => useState<Me | null>('me', () => null)
