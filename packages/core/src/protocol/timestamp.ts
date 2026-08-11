import { z } from "zod";

const timestampPattern =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})Z$/;
export const timestampSchema = z
  .string()
  .regex(timestampPattern)
  .refine((value) => {
    const match = timestampPattern.exec(value);
    if (match === null) return false;
    const [, yearText, monthText, dayText, hourText, minuteText, secondText] =
      match;
    const year = Number(yearText);
    const month = Number(monthText);
    const day = Number(dayText);
    const hour = Number(hourText);
    const minute = Number(minuteText);
    const second = Number(secondText);
    if (
      year < 1 ||
      year > 9999 ||
      month < 1 ||
      month > 12 ||
      hour > 23 ||
      minute > 59 ||
      second > 59
    )
      return false;
    const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
    return day >= 1 && day <= days;
  });

export type Timestamp = z.infer<typeof timestampSchema>;
