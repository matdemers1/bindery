/** The throttle names a delay; saying so beats a form that just stops working. */
export function retryMessage(retryAfter: number | undefined): string {
  const seconds = retryAfter ?? 60;
  return `Too many attempts. Try again in ${
    seconds < 90 ? `${seconds} seconds` : `${Math.ceil(seconds / 60)} minutes`
  }.`;
}
