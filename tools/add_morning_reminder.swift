import EventKit
import Foundation

let title = "Open and review the Orange Watch dashboard"
let store = EKEventStore()
let sem = DispatchSemaphore(value: 0)
var granted = false
store.requestFullAccessToReminders { ok, err in
    granted = ok
    if let err = err { print("ACCESS_ERROR: \(err.localizedDescription)") }
    sem.signal()
}
sem.wait()
guard granted else { print("NO_ACCESS"); exit(2) }

// Skip if it already exists (incomplete, same title).
let pred = store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: nil)
var existing: [EKReminder] = []
let sem2 = DispatchSemaphore(value: 0)
store.fetchReminders(matching: pred) { r in existing = (r ?? []).filter { $0.title == title }; sem2.signal() }
sem2.wait()
if let r = existing.first {
    print("EXISTS in list '\(r.calendar.title)', due \(r.dueDateComponents?.date.map { "\($0)" } ?? "?")")
    exit(0)
}

guard let cal = store.defaultCalendarForNewReminders() else { print("NO_DEFAULT_LIST"); exit(3) }
let r = EKReminder(eventStore: store)
r.title = title
r.notes = "Orange Watch runs on your Mac: in Terminal, cd \"~/Documents/Claude Projects/oc-osint-dashboard\" && python3 server.py, then open http://localhost:8787"
r.calendar = cal

// Next 5:00 AM local time.
var c = Calendar.current
c.timeZone = TimeZone.current
let now = Date()
var due = c.date(bySettingHour: 5, minute: 0, second: 0, of: now)!
if due <= now { due = c.date(byAdding: .day, value: 1, to: due)! }
var comps = c.dateComponents([.year, .month, .day, .hour, .minute], from: due)
comps.timeZone = TimeZone.current
r.dueDateComponents = comps
r.startDateComponents = comps
r.addAlarm(EKAlarm(absoluteDate: due))
r.addRecurrenceRule(EKRecurrenceRule(recurrenceWith: .daily, interval: 1, end: nil))

do {
    try store.save(r, commit: true)
    print("CREATED in list '\(cal.title)' (source: \(cal.source.title)), first due \(due), repeats daily")
} catch {
    print("SAVE_ERROR: \(error.localizedDescription)"); exit(4)
}
