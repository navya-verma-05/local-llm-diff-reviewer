export interface User {
  id: string;
  email: string;
}

export async function findUser(id: string): Promise<User | null> {
  if (!id) {
    return null;
  }
  return { id, email: "user@example.com" };
}
